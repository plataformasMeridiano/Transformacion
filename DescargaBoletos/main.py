"""
main.py — Orquestador diario de descarga y subida de boletos.

Uso:
    python3 main.py              # procesa ayer (today - days_back)
    python3 main.py 2026-01-27   # procesa una fecha específica (debug/reprocess)
"""

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from drive_uploader import DriveUploader, nro_from_filename
from supabase_logger import (
    log_boleto,
    start_corrida, finish_corrida,
    start_alyc_detalle, finish_alyc_detalle,
)
from scrapers.alyc_sistemaA import PuenteScraper
from scrapers.alyc_sistemaB import AdcapScraper
from scrapers.alyc_sistemaC import WinScraper
from scrapers.alyc_sistemaD import ConoSurScraper
from scrapers.alyc_sistemaE import MaxCapitalScraper
from scrapers.alyc_sistemaF import MetroCorpScraper
from scrapers.alyc_sistemaG import DhalmoreScraper
from scrapers.alyc_sistemaH import AllariaScraper

# ── Mapa sistema → clase scraper ──────────────────────────────────────────────
SCRAPER_MAP = {
    "sistemaA": PuenteScraper,
    "sistemaB": AdcapScraper,
    "sistemaC": WinScraper,
    "sistemaD": ConoSurScraper,
    "sistemaE": MaxCapitalScraper,
    "sistemaF": MetroCorpScraper,
    "sistemaG": DhalmoreScraper,
    "sistemaH": AllariaScraper,
}


# ── Resultado por ALYC ────────────────────────────────────────────────────────
@dataclass
class AlycResult:
    nombre: str
    descargados: int = 0
    subidos: int = 0
    errores_upload: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def total_errores(self) -> int:
        return self.errores_upload + (0 if self.ok else 1)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _resolve_env(value: str) -> str:
    """Expande ${VAR} con variables de entorno; lanza EnvironmentError si falta alguna."""
    def replacer(m: re.Match) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            raise EnvironmentError(f"Variable de entorno '{var}' no definida en .env")
        return val
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def setup_logging(log_dir: str, fecha: str) -> None:
    log_path = Path(log_dir) / f"{fecha}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(ch)


# ── Procesamiento por ALYC ────────────────────────────────────────────────────
async def process_alyc(
    alyc_cfg: dict,
    general_cfg: dict,
    fecha: str,
    uploader: DriveUploader,
) -> AlycResult:
    nombre = alyc_cfg["nombre"]
    sistema = alyc_cfg["sistema"]
    logger = logging.getLogger(f"alyc.{nombre}")
    result = AlycResult(nombre=nombre)

    # Verificar que el sistema tenga scraper implementado
    cls = SCRAPER_MAP.get(sistema)
    if cls is None:
        result.error = f"Sistema '{sistema}' no tiene scraper implementado"
        logger.error(result.error)
        return result

    dest_dir = Path(general_cfg["download_dir"]) / nombre / fecha
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with cls(alyc_cfg, general_cfg) as scraper:
            # ── Login ──────────────────────────────────────────────────────
            logger.info("Iniciando login")
            await scraper.login()
            logger.info("Login exitoso")

            # ── Descarga ───────────────────────────────────────────────────
            logger.info("Descargando boletos para %s → %s", fecha, dest_dir)
            pdfs = await scraper.download_tickets(fecha, dest_dir)
            result.descargados = len(pdfs)
            logger.info("%d boleto(s) descargado(s)", len(pdfs))

            # ── Subida a Drive ─────────────────────────────────────────────
            for pdf_path in pdfs:
                # La subcarpeta directa del archivo indica el tipo de operación
                tipo_operacion = pdf_path.parent.name
                nro_boleto = nro_from_filename(pdf_path.name)
                try:
                    file_id = uploader.upload_boleto(
                        pdf_path=pdf_path,
                        tipo_operacion=tipo_operacion,
                        fecha=fecha,
                        alyc_nombre=nombre,
                        nro_boleto=nro_boleto,
                    )
                    logger.info(
                        "Drive OK  %-30s  tipo=%-10s  id=%s",
                        pdf_path.name, tipo_operacion, file_id,
                    )
                    result.subidos += 1
                    log_boleto(fecha, nombre, tipo_operacion, nro_boleto, pdf_path.name, file_id)
                except Exception as exc:
                    logger.error(
                        "Drive FALLÓ  %s — %s: %s",
                        pdf_path.name, type(exc).__name__, exc,
                    )
                    result.errores_upload += 1

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error("ALYC falló — %s", result.error)

    return result


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> int:
    # Cargar .env desde el mismo directorio que este script
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = config["general"]

    # Fecha a procesar: CLI arg o ayer según days_back
    if len(sys.argv) > 1:
        fecha = sys.argv[1]
    else:
        days_back = general.get("days_back", 1)
        fecha = (date.today() - timedelta(days=days_back)).isoformat()

    # Logging
    setup_logging(general["log_dir"], fecha)
    logger = logging.getLogger("main")
    logger.info("=" * 60)
    logger.info("DescargaBoletos iniciado — fecha: %s", fecha)

    # Forzar headless=True para ejecución productiva
    general = {**general, "headless": True}

    # Drive uploader
    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id,
                             tipo_folder_overrides=gd.get("tipo_folder_overrides"))
    logger.info("DriveUploader listo (root_folder=%s)", root_folder_id)

    # ALYCs activas
    alycs = [a for a in config["alycs"] if a.get("activo", False)]
    logger.info("ALYCs a procesar: %s", [a["nombre"] for a in alycs])

    if not alycs:
        logger.warning("No hay ALYCs activas en config.json — nada que hacer")
        return 0

    # ── Registrar inicio de corrida en Supabase ───────────────────────────
    alyc_nombres = [a["nombre"] for a in alycs]
    corrida_id = start_corrida(fecha, alycs=alyc_nombres)
    if corrida_id:
        logger.info("Corrida registrada en Supabase — id=%s", corrida_id)
    else:
        logger.warning("No se pudo registrar la corrida en Supabase (continúa igual)")

    # Procesar cada ALYC secuencialmente (un browser a la vez)
    resultados: list[AlycResult] = []
    for alyc_cfg in alycs:
        logger.info("-" * 40)
        logger.info("Procesando: %s (%s)", alyc_cfg["nombre"], alyc_cfg["sistema"])

        detalle_id = None
        if corrida_id:
            detalle_id = start_alyc_detalle(corrida_id, alyc_cfg["nombre"], alyc_cfg["sistema"])

        result = await process_alyc(alyc_cfg, general, fecha, uploader)
        resultados.append(result)

        if detalle_id:
            finish_alyc_detalle(
                detalle_id,
                desc_count=result.descargados,
                sub_count=result.subidos,
                err_count=result.total_errores,
                estado="ok" if result.ok else "error",
                error_detalle=result.error,
            )

    # ── Resumen final ─────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("RESUMEN — %s", fecha)
    logger.info("%-22s  %-6s  %-12s  %-8s  %-14s  %s",
                "ALYC", "Estado", "Descargados", "Subidos", "Err.Upload", "Detalle")
    logger.info("-" * 90)

    total_desc = total_sub = total_err = 0
    for r in resultados:
        estado = "OK" if r.ok else "ERROR"
        detalle = r.error or ""
        logger.info("%-22s  %-6s  %-12d  %-8d  %-14d  %s",
                    r.nombre, estado, r.descargados, r.subidos, r.errores_upload, detalle)
        total_desc += r.descargados
        total_sub  += r.subidos
        total_err  += r.total_errores

    logger.info("-" * 90)
    logger.info("%-22s  %-6s  %-12d  %-8d  %-14d",
                "TOTAL", "", total_desc, total_sub, total_err)
    logger.info("=" * 60)

    # ── Registrar fin de corrida en Supabase ──────────────────────────────
    if corrida_id:
        estado_final = "completado" if total_err == 0 else "error"
        finish_corrida(corrida_id, total_desc, total_sub, total_err, estado=estado_final)

    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

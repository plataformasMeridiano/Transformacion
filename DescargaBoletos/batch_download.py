"""
batch_download.py — Descarga histórica para un rango de fechas.

Estrategia: una sesión de browser por ALYC (un solo login), luego llama
download_tickets() para cada fecha dentro de la misma sesión.
Esto evita ~34 logins por ALYC y reduce el tiempo total a ~2-3 hs.

Los archivos ya subidos a Drive se saltean automáticamente (dedup en DriveUploader).

Uso:
    python3 batch_download.py                          # rango hardcodeado abajo
    python3 batch_download.py 2026-01-15 2026-03-03   # rango por CLI
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from drive_uploader import DriveUploader, nro_from_filename
from supabase_logger import log_boleto
from scrapers.alyc_sistemaA import PuenteScraper
from scrapers.alyc_sistemaB import AdcapScraper
from scrapers.alyc_sistemaC import WinScraper
from scrapers.alyc_sistemaD import ConoSurScraper
from scrapers.alyc_sistemaE import MaxCapitalScraper
from scrapers.alyc_sistemaF import MetroCorpScraper
from scrapers.alyc_sistemaG import DhalmoreScraper
from scrapers.alyc_sistemaH import AllariaScraper

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

SKIP_ALYCS: set[str] = set()


def _resolve_env(value: str) -> str:
    def replacer(m):
        val = os.environ.get(m.group(1))
        if val is None:
            raise EnvironmentError(f"Variable '{m.group(1)}' no definida")
        return val
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def business_days(start: date, end: date) -> list[str]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def setup_logging(log_dir: str, tag: str) -> None:
    log_path = Path(log_dir) / f"batch_{tag}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(ch)


async def process_alyc_batch(
    alyc_cfg: dict,
    general_cfg: dict,
    fechas: list[str],
    uploader: DriveUploader,
) -> tuple[int, int, int]:
    """
    Abre UNA sesión para la ALYC y procesa todas las fechas dentro de ella.
    Retorna (descargados, subidos, errores).
    """
    nombre = alyc_cfg["nombre"]
    logger = logging.getLogger(f"batch.{nombre}")

    cls = SCRAPER_MAP.get(alyc_cfg["sistema"])
    if cls is None:
        logger.error("Sistema '%s' sin scraper — omitiendo", alyc_cfg["sistema"])
        return 0, 0, 1

    total_desc = total_sub = total_err = 0

    try:
        async with cls(alyc_cfg, general_cfg) as scraper:
            logger.info("=" * 50)
            logger.info("[%s] Iniciando login", nombre)
            await scraper.login()
            logger.info("[%s] Login OK — procesando %d fechas", nombre, len(fechas))

            for fecha in fechas:
                dest_dir = Path(general_cfg["download_dir"]) / nombre / fecha
                dest_dir.mkdir(parents=True, exist_ok=True)

                try:
                    pdfs = await scraper.download_tickets(fecha, dest_dir)
                    total_desc += len(pdfs)

                    for pdf_path in pdfs:
                        tipo = pdf_path.parent.name
                        nro  = nro_from_filename(pdf_path.name)
                        try:
                            file_id = uploader.upload_boleto(pdf_path, tipo, fecha, nombre, nro,
                                                             overwrite=True)
                            total_sub += 1
                            log_boleto(fecha, nombre, tipo, nro, pdf_path.name, file_id)
                        except Exception as exc:
                            logger.error("[%s] Upload %s falló: %s", nombre, pdf_path.name, exc)
                            total_err += 1

                except Exception as exc:
                    logger.error("[%s] Fecha %s falló — %s: %s",
                                 nombre, fecha, type(exc).__name__, exc)
                    total_err += 1

    except Exception as exc:
        logger.error("[%s] Sesión abortada — %s: %s", nombre, type(exc).__name__, exc)
        total_err += 1

    logger.info("[%s] TOTAL: desc=%d  sub=%d  err=%d", nombre, total_desc, total_sub, total_err)
    return total_desc, total_sub, total_err


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = config["general"]

    # Rango de fechas: CLI args o valores por defecto
    if len(sys.argv) == 3:
        inicio = date.fromisoformat(sys.argv[1])
        fin    = date.fromisoformat(sys.argv[2])
    else:
        inicio = date(2026, 1, 15)
        fin    = date(2026, 3, 3)

    fechas = business_days(inicio, fin)

    tag = f"{inicio.isoformat()}_a_{fin.isoformat()}"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("batch")
    logger.info("=" * 60)
    logger.info("batch_download — %s a %s (%d días hábiles)", inicio, fin, len(fechas))

    # headless=True para todo excepto los que lo sobreescriben (MaxCapital, MetroCorp)
    general = {**general, "headless": True}

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id,
                             tipo_folder_overrides=gd.get("tipo_folder_overrides"))

    alycs = [
        a for a in config["alycs"]
        if a.get("activo") and a["nombre"] not in SKIP_ALYCS
    ]
    logger.info("ALYCs: %s", [a["nombre"] for a in alycs])
    if not fechas:
        logger.info("Sin días hábiles en el rango — nada que hacer")
        return 0
    logger.info("Fechas: %s … %s", fechas[0], fechas[-1])

    grand_desc = grand_sub = grand_err = 0
    for alyc_cfg in alycs:
        desc, sub, err = await process_alyc_batch(alyc_cfg, general, fechas, uploader)
        grand_desc += desc
        grand_sub  += sub
        grand_err  += err

    logger.info("=" * 60)
    logger.info("RESUMEN FINAL: desc=%d  sub=%d  err=%d", grand_desc, grand_sub, grand_err)
    logger.info("=" * 60)
    return 0 if grand_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

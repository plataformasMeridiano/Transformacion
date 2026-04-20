"""
batch_download.py — Descarga histórica / delta de boletos.

Estrategia: una sesión de browser por ALYC (un solo login), luego llama
download_tickets() para cada fecha dentro de la misma sesión.

Cada (fecha, ALYC) procesada se registra en Supabase
(descargas_cauciones_corridas_log + detalle), lo que permite el modo --delta:
detectar automáticamente qué fechas faltan y procesarlas.

Uso:
    python3 batch_download.py --delta                      # fechas faltantes (max 7 días)
    python3 batch_download.py --delta --mas-una-semana     # fechas faltantes sin límite
    python3 batch_download.py 2026-04-17                   # una fecha exacta
    python3 batch_download.py 2026-04-10 2026-04-17        # rango explícito
"""

import argparse
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
from supabase_logger import (
    log_boleto,
    start_corrida, finish_corrida,
    start_alyc_detalle, finish_alyc_detalle,
    get_fechas_completadas,
)
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

_DELTA_WINDOW_DAYS = 90   # cuánto tiempo atrás buscar en modo --delta


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


def _find_delta_fechas(alycs: list[dict]) -> list[str]:
    """
    Consulta Supabase y retorna todas las fechas hábiles faltantes
    (sin corrida completa para todas las ALYCs activas) en los últimos
    _DELTA_WINDOW_DAYS días calendario.

    El caller decide si el largo de la lista supera algún límite.
    """
    logger = logging.getLogger("batch")

    # Nombres únicos de ALYCs activas (ConoSur aparece dos veces en config)
    unique_nombres = list(dict.fromkeys(
        a["nombre"] for a in alycs if a.get("activo") and a["nombre"] not in SKIP_ALYCS
    ))
    alyc_set = set(unique_nombres)

    window_start = date.today() - timedelta(days=_DELTA_WINDOW_DAYS)
    completadas = get_fechas_completadas(unique_nombres, window_start.isoformat())

    # Última fecha donde TODAS las ALYCs tienen estado=ok
    ok_dates = sorted(
        fecha for fecha, ok_alycs in completadas.items()
        if alyc_set.issubset(ok_alycs)
    )
    last_ok = date.fromisoformat(ok_dates[-1]) if ok_dates else None

    if last_ok:
        logger.info("Delta: última fecha completa = %s", last_ok)
        delta_start = last_ok + timedelta(days=1)
    else:
        logger.info("Delta: sin fecha completa en los últimos %d días — usando %s",
                    _DELTA_WINDOW_DAYS, window_start)
        delta_start = window_start

    yesterday = date.today() - timedelta(days=1)
    todos = business_days(delta_start, yesterday)

    # Descartar fechas que ya están completas (parcialmente recuperadas)
    faltantes = [
        f for f in todos
        if not alyc_set.issubset(completadas.get(f, set()))
    ]

    if faltantes:
        logger.info("Delta: %d fecha(s) faltante(s): %s … %s",
                    len(faltantes), faltantes[0], faltantes[-1])
    else:
        logger.info("Delta: todo al día — no hay fechas faltantes")

    return faltantes


async def process_alyc_batch(
    alyc_cfg: dict,
    general_cfg: dict,
    fechas: list[str],
    uploader: DriveUploader,
) -> tuple[int, int, int]:
    """
    Abre UNA sesión para la ALYC y procesa todas las fechas dentro de ella.
    Registra una corrida + detalle en Supabase por cada (fecha, ALYC).
    Retorna (descargados, subidos, errores).
    """
    nombre = alyc_cfg["nombre"]
    sistema = alyc_cfg["sistema"]
    logger = logging.getLogger(f"batch.{nombre}")

    cls = SCRAPER_MAP.get(sistema)
    if cls is None:
        logger.error("Sistema '%s' sin scraper — omitiendo", sistema)
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

                # Registrar inicio de corrida para esta (fecha, ALYC)
                corrida_id = start_corrida(fecha, alycs=[nombre])
                detalle_id = (
                    start_alyc_detalle(corrida_id, nombre, sistema)
                    if corrida_id else None
                )

                fd = fs = fe = 0
                fecha_error: str | None = None

                try:
                    pdfs = await scraper.download_tickets(fecha, dest_dir)
                    fd = len(pdfs)
                    total_desc += fd

                    for pdf_path in pdfs:
                        tipo = pdf_path.parent.name
                        nro  = nro_from_filename(pdf_path.name)
                        try:
                            file_id = uploader.upload_boleto(
                                pdf_path, tipo, fecha, nombre, nro, overwrite=True,
                            )
                            fs += 1
                            total_sub += 1
                            log_boleto(fecha, nombre, tipo, nro, pdf_path.name, file_id)
                        except Exception as exc:
                            logger.error("[%s] Upload %s falló: %s", nombre, pdf_path.name, exc)
                            fe += 1
                            total_err += 1

                except Exception as exc:
                    fecha_error = f"{type(exc).__name__}: {exc}"
                    logger.error("[%s] Fecha %s falló — %s", nombre, fecha, fecha_error)
                    fe += 1
                    total_err += 1

                finally:
                    estado = "ok" if fecha_error is None and fe == 0 else "error"
                    if detalle_id:
                        finish_alyc_detalle(
                            detalle_id, fd, fs, fe,
                            estado=estado,
                            error_detalle=fecha_error,
                        )
                    if corrida_id:
                        finish_corrida(
                            corrida_id, fd, fs, fe,
                            estado="completado" if estado == "ok" else "error",
                        )

    except Exception as exc:
        logger.error("[%s] Sesión abortada — %s: %s", nombre, type(exc).__name__, exc)
        total_err += 1

    logger.info("[%s] TOTAL: desc=%d  sub=%d  err=%d", nombre, total_desc, total_sub, total_err)
    return total_desc, total_sub, total_err


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descarga de boletos — rango explícito o delta automático",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  batch_download.py --delta\n"
            "  batch_download.py --delta --mas-una-semana\n"
            "  batch_download.py 2026-04-17\n"
            "  batch_download.py 2026-04-10 2026-04-17\n"
        ),
    )
    parser.add_argument(
        "--delta", action="store_true",
        help="Detectar y procesar fechas faltantes (máx 7 días sin --mas-una-semana)",
    )
    parser.add_argument(
        "--mas-una-semana", dest="mas_una_semana", action="store_true",
        help="Con --delta: procesar más de 7 días de atraso sin confirmación",
    )
    parser.add_argument(
        "rango", nargs="*",
        metavar="FECHA",
        help="Una fecha (YYYY-MM-DD) o rango inicio fin",
    )
    return parser.parse_args()


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = config["general"]
    # headless=True para todo excepto los que lo sobreescriben en opciones
    general = {**general, "headless": True}

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"], root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    alycs = [
        a for a in config["alycs"]
        if a.get("activo") and a["nombre"] not in SKIP_ALYCS
    ]

    args = _parse_args()

    # Logging preliminar (stdout) hasta que tengamos el tag para configurar el archivo
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Determinar fechas a procesar ──────────────────────────────────────
    if args.delta:
        fechas = _find_delta_fechas(alycs)
        if not fechas:
            return 0  # todo al día
        if not args.mas_una_semana and len(fechas) > 7:
            # El logger todavía no está configurado; escribir directo a stderr
            print(
                f"ERROR: delta tiene {len(fechas)} fechas faltantes (> 7 días). "
                "Usar --mas-una-semana para procesar sin límite.",
                file=sys.stderr,
            )
            return 1
        tag = f"delta_{fechas[0]}_a_{fechas[-1]}"
        inicio = date.fromisoformat(fechas[0])
        fin    = date.fromisoformat(fechas[-1])

    elif len(args.rango) == 1:
        inicio = fin = date.fromisoformat(args.rango[0])
        fechas = business_days(inicio, fin)
        tag = inicio.isoformat()

    elif len(args.rango) == 2:
        inicio = date.fromisoformat(args.rango[0])
        fin    = date.fromisoformat(args.rango[1])
        fechas = business_days(inicio, fin)
        tag = f"{inicio.isoformat()}_a_{fin.isoformat()}"

    else:
        # Legado: sin argumentos → rango hardcodeado (para compatibilidad)
        inicio = date(2026, 1, 15)
        fin    = date(2026, 3, 3)
        fechas = business_days(inicio, fin)
        tag = f"{inicio.isoformat()}_a_{fin.isoformat()}"

    # ── Logging ───────────────────────────────────────────────────────────
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("batch")
    logger.info("=" * 60)
    logger.info("batch_download — %s a %s (%d días hábiles)", inicio, fin, len(fechas))
    logger.info("ALYCs: %s", [a["nombre"] for a in alycs])

    if not fechas:
        logger.info("Sin días hábiles en el rango — nada que hacer")
        return 0

    logger.info("Fechas: %s … %s", fechas[0], fechas[-1])

    # ── Procesar ──────────────────────────────────────────────────────────
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

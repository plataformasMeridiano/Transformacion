"""
redownload_metro.py — Re-descarga boletos MetroCorp con el endpoint correcto.

Los boletos previos (descargados con metrocorp.downloadList) son incorrectos.
Este script re-descarga todo el rango con metrocorp.detail + metrocorp.downloadDetail
y sobreescribe los archivos existentes en Drive.

Uso:
    python3 redownload_metro.py                          # rango hardcodeado
    python3 redownload_metro.py 2026-01-15 2026-03-03   # rango por CLI
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
from scrapers.alyc_sistemaF import MetroCorpScraper

# ── Configuración ──────────────────────────────────────────────────────────────

START_DATE = date(2026, 1, 15)
END_DATE   = date(2026, 3, 3)


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


def setup_logging(log_dir: str) -> None:
    log_path = Path(log_dir) / "redownload_metro.log"
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
        inicio = START_DATE
        fin    = END_DATE

    fechas = business_days(inicio, fin)

    setup_logging(general["log_dir"])
    logger = logging.getLogger("redownload_metro")
    logger.info("=" * 60)
    logger.info("redownload_metro — %s a %s (%d días hábiles)", inicio, fin, len(fechas))

    # MetroCorp requiere headless=False
    general = {**general, "headless": False}

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    # Obtener config MetroCorp
    metro_cfg = next(
        (a for a in config["alycs"] if a.get("sistema") == "sistemaF" and a.get("activo")),
        None,
    )
    if not metro_cfg:
        logger.error("No se encontró configuración activa para MetroCorp (sistemaF)")
        return 1

    nombre = metro_cfg["nombre"]
    total_desc = total_sub = total_err = 0

    for fecha in fechas:
        dest_dir = Path(general["download_dir"]) / nombre / fecha
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            async with MetroCorpScraper(metro_cfg, general) as scraper:
                logger.info("[%s] Login para %s…", nombre, fecha)
                await scraper.login()
                pdfs = await scraper.download_tickets(fecha, dest_dir)
                total_desc += len(pdfs)

            for pdf_path in pdfs:
                tipo = pdf_path.parent.name
                nro  = nro_from_filename(pdf_path.name)
                try:
                    uploader.upload_boleto(
                        pdf_path, tipo, fecha, nombre, nro,
                        overwrite=True,
                    )
                    total_sub += 1
                except Exception as exc:
                    logger.error("[%s] Upload %s falló: %s", nombre, pdf_path.name, exc)
                    total_err += 1

        except Exception as exc:
            logger.error("[%s] Fecha %s falló — %s: %s",
                         nombre, fecha, type(exc).__name__, exc)
            total_err += 1

    logger.info("=" * 60)
    logger.info("[%s] TOTAL: desc=%d  sub=%d  err=%d", nombre, total_desc, total_sub, total_err)
    logger.info("=" * 60)
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

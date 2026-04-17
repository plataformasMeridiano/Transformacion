"""
run_dhalmore_viernes.py — Backfill Dhalmore para todos los viernes desde 2026-01-01.

Motivación: el scraper original usaba toDate=fecha+1 en historical-movements, que
filtra por fecha de LIQUIDACIÓN. Las cauciones TERMINO (multi-día) liquidaban
después y sus boletos se perdían. El fix amplía el rango a fecha+60.

Este script re-corre solo Dhalmore, solo viernes, para recuperar los boletos TERMINO
que faltaban. Los CONTADO (ya descargados) se saltean por existir localmente.
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

from batch_download import process_alyc_batch, setup_logging
from drive_uploader import DriveUploader, nro_from_filename

load_dotenv(Path(__file__).parent / ".env")


def _resolve_env(value: str) -> str:
    def replacer(m):
        val = os.environ.get(m.group(1))
        if val is None:
            raise EnvironmentError(f"Variable '{m.group(1)}' no definida")
        return val
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def fridays(start: date, end: date) -> list[str]:
    """Devuelve todos los viernes (weekday=4) entre start y end inclusive."""
    days, d = [], start
    while d <= end:
        if d.weekday() == 4:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


async def main() -> int:
    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = config["general"]
    inicio  = date(2026, 1, 1)
    fin     = date.today()
    fechas  = fridays(inicio, fin)

    setup_logging(general["log_dir"], "dhalmore_viernes")
    logger = logging.getLogger("dhalmore_viernes")
    logger.info("=" * 60)
    logger.info("Backfill Dhalmore viernes — %s a %s (%d fechas)", inicio, fin, len(fechas))
    for f in fechas:
        logger.info("  %s", f)

    general = {**general, "headless": False}  # Dhalmore requiere headless=False

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    dhalmore_cfg = next(
        a for a in config["alycs"] if a["nombre"] == "Dhalmore"
    )

    desc, sub, err = await process_alyc_batch(dhalmore_cfg, general, fechas, uploader)

    logger.info("=" * 60)
    logger.info("RESUMEN: desc=%d  sub=%d  err=%d", desc, sub, err)
    logger.info("=" * 60)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

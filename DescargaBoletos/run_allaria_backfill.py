"""
run_allaria_backfill.py — Backfill Allaria desde 2025-01-27 hasta hoy.

Descarga boletos de Venta FCE-eCheq y Cauciones y los sube a Drive.
"""
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from batch_download import _resolve_env, business_days, process_alyc_batch, setup_logging
from drive_uploader import DriveUploader


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = {**config["general"], "headless": False}

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    tag = "allaria_backfill"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger(tag)

    alyc = next(a for a in config["alycs"] if a["nombre"] == "Allaria")

    inicio = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 4, 1)
    fechas = business_days(inicio, date.today())
    fechas = list(reversed(fechas))

    logger.info("=" * 60)
    logger.info("Allaria backfill — %d fechas hábiles", len(fechas))
    logger.info("Rango: %s → %s", fechas[-1], fechas[0])

    desc, sub, err = await process_alyc_batch(alyc, general, fechas, uploader)

    logger.info("=" * 60)
    logger.info("TOTAL: desc=%d  sub=%d  err=%d", desc, sub, err)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

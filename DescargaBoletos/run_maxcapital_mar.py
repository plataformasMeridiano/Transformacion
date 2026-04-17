"""
run_maxcapital_mar.py — Retry MaxCapital para 2026-03-01 a 2026-03-12.
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

    # headless se toma de opciones.headless del scraper (False para MaxCapital)
    general = {**config["general"], "headless": True}

    inicio = date(2026, 3, 2)   # primer día hábil del rango
    fin    = date(2026, 3, 12)
    fechas = business_days(inicio, fin)

    tag = f"maxcapital_{inicio.isoformat()}_a_{fin.isoformat()}"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("run_maxcapital")
    logger.info("=" * 60)
    logger.info("MaxCapital retry — %s a %s (%d días hábiles)", inicio, fin, len(fechas))

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    alyc_cfg = next(a for a in config["alycs"] if a["nombre"] == "MaxCapital")

    desc, sub, err = await process_alyc_batch(alyc_cfg, general, fechas, uploader)
    logger.info("=" * 60)
    logger.info("RESULTADO: desc=%d  sub=%d  err=%d", desc, sub, err)
    logger.info("=" * 60)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

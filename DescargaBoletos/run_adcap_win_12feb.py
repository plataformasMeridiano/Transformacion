"""
run_adcap_win_12feb.py — Descarga ADCAP (Cauciones) y WIN (Pases) para 2026-02-12.

Faltantes detectados en Drive:
  - ADCAP Cauciones 2026-02-12: 0 archivos
  - WIN   Pases     2026-02-12: 0 archivos
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from batch_download import _resolve_env, process_alyc_batch, setup_logging
from drive_uploader import DriveUploader


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = {**config["general"], "headless": False}
    fechas = ["2026-02-12"]

    tag = "adcap_win_12feb"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger(tag)

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    total_desc = total_sub = total_err = 0

    for nombre in ["ADCAP", "WIN"]:
        alyc = next((a for a in config["alycs"] if a.get("activo") and a["nombre"] == nombre), None)
        if not alyc:
            logger.error("No se encontró %s activo en config.json", nombre)
            continue

        logger.info("=" * 60)
        logger.info("%s — fecha: %s", nombre, fechas[0])
        desc, sub, err = await process_alyc_batch(alyc, general, fechas, uploader)
        logger.info("%s — desc=%d  sub=%d  err=%d", nombre, desc, sub, err)
        total_desc += desc
        total_sub += sub
        total_err += err

    logger.info("=" * 60)
    logger.info("TOTAL: desc=%d  sub=%d  err=%d", total_desc, total_sub, total_err)
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

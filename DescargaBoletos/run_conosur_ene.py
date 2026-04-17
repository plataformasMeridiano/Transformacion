"""
run_conosur_ene.py — Descarga ConoSur (MeridianoNorte + Pamat) para ene 15-30 2026.
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

    general = {**config["general"], "headless": True}

    inicio = date(2026, 1, 15)
    fin    = date(2026, 1, 30)
    fechas = business_days(inicio, fin)

    tag = f"conosur_{inicio.isoformat()}_a_{fin.isoformat()}"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("conosur_batch")
    logger.info("=" * 60)
    logger.info("ConoSur batch — %s a %s (%d días hábiles)", inicio, fin, len(fechas))
    logger.info("Fechas: %s", fechas)

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    alycs = [a for a in config["alycs"] if a.get("activo") and a["nombre"] == "ConoSur"]
    logger.info("Entradas ConoSur en config: %d", len(alycs))
    for a in alycs:
        logger.info("  cuenta=%s  usuario=%s", a["opciones"]["cuenta"], a["usuario"])

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

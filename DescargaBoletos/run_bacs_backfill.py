"""
run_bacs_backfill.py — Descarga histórica de boletos BACS (Toronto Inversiones)
para el rango ene-15 a feb-28 (los previos al batch de mar-01 a mar-12).

BACS config: caucion_codes=[] → todo clasificado como Pases.
             tipo_operacion=["Pases"] → descarga todo.
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
    fin    = date(2026, 2, 28)
    fechas = business_days(inicio, fin)

    tag = f"bacs_backfill_{inicio.isoformat()}_a_{fin.isoformat()}"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("bacs_backfill")
    logger.info("=" * 60)
    logger.info("BACS backfill — %s a %s (%d días hábiles)", inicio, fin, len(fechas))

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    alycs = [a for a in config["alycs"] if a.get("activo") and a["nombre"] == "BACS"]
    if not alycs:
        logger.error("No se encontró BACS activo en config.json")
        return 1
    logger.info("BACS encontrado: %d entrada(s)", len(alycs))

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

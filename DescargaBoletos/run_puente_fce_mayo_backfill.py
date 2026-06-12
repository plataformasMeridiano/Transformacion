"""
run_puente_fce_mayo_backfill.py — Descarga boletos Venta FCE-eCheq de Puente para mayo 2026.

Solo procesa el tipo "Venta FCE-eCheq" para no re-descargar Cauciones/Pases ya existentes.
"""
import asyncio
import copy
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

    inicio = date(2026, 5, 1)
    fin    = date(2026, 5, 30)
    fechas = business_days(inicio, fin)

    tag = "puente_fce_mayo_backfill"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("puente_fce_mayo_backfill")
    logger.info("=" * 60)
    logger.info("Puente FCE backfill — %s a %s (%d días hábiles)", inicio, fin, len(fechas))

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    alycs = [a for a in config["alycs"] if a.get("activo") and a["nombre"] == "Puente"]
    if not alycs:
        logger.error("No se encontró Puente activo en config.json")
        return 1

    # Solo procesar FCE — no re-descargar Cauciones/Pases
    alyc_cfg = copy.deepcopy(alycs[0])
    alyc_cfg["opciones"]["tipo_operacion"] = ["Venta FCE-eCheq"]

    desc, sub, err = await process_alyc_batch(alyc_cfg, general, fechas, uploader)

    logger.info("=" * 60)
    logger.info("RESUMEN FINAL: desc=%d  sub=%d  err=%d", desc, sub, err)
    logger.info("=" * 60)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

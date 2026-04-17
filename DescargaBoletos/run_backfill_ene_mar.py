"""
run_backfill_ene_mar.py

Backfill completo 15-ene-2026 → 12-mar-2026, en orden INVERSO (más reciente primero).
Procesa todas las ALYCs activas. Los archivos ya en Drive se saltean automáticamente.
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
    fin    = date(2026, 3, 12)
    fechas = list(reversed(business_days(inicio, fin)))  # 12-mar → 15-ene

    tag = f"backfill_{inicio.isoformat()}_a_{fin.isoformat()}_inverso"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("backfill")
    logger.info("=" * 60)
    logger.info("Backfill %s → %s (%d fechas hábiles, orden inverso)", fin, inicio, len(fechas))

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    alycs = [a for a in config["alycs"] if a.get("activo")]
    logger.info("ALYCs: %s", [a["nombre"] for a in alycs])

    PARALELO = 3  # ALYCs corriendo simultáneamente

    grand_desc = grand_sub = grand_err = 0

    for i in range(0, len(alycs), PARALELO):
        grupo = alycs[i:i + PARALELO]
        logger.info("--- Grupo %d: %s", i // PARALELO + 1, [a["nombre"] for a in grupo])
        resultados = await asyncio.gather(
            *[process_alyc_batch(a, general, fechas, uploader) for a in grupo]
        )
        for desc, sub, err in resultados:
            grand_desc += desc
            grand_sub  += sub
            grand_err  += err

    logger.info("=" * 60)
    logger.info("RESUMEN FINAL: desc=%d  sub=%d  err=%d", grand_desc, grand_sub, grand_err)
    logger.info("=" * 60)
    return 0 if grand_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
run_fce_abril_backfill.py — Descarga boletos Venta FCE-eCheq 2026-04-13 al 2026-04-27.

ALYCs: Allaria, ADCAP, DAValores, IEB, Dhalmore
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from batch_download import _resolve_env, process_alyc_batch, setup_logging
from drive_uploader import DriveUploader

load_dotenv(Path(__file__).parent / ".env")

# Días hábiles 13-27 de abril
FECHAS_TODAS = [
    "2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16", "2026-04-17",
    "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24",
    "2026-04-27",
]

# Excluir fechas ya descargadas para evitar duplicados en Drive
FECHAS_POR_ALYC = {
    "Allaria":   FECHAS_TODAS,
    "ADCAP":     [f for f in FECHAS_TODAS if f != "2026-04-13"],   # 13 ya descargado
    "DAValores": FECHAS_TODAS,
    "IEB":       [f for f in FECHAS_TODAS if f != "2026-04-24"],   # 24 ya descargado
    "Dhalmore":  [f for f in FECHAS_TODAS if f != "2026-04-27"],   # 27 ya descargado
}


async def main() -> int:
    cfg = json.loads(Path("config.json").read_text())
    general = {**cfg["general"], "headless": False}

    gd = cfg["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    setup_logging(general["log_dir"], "fce_abril_backfill")
    logger = logging.getLogger("fce_abril_backfill")

    alycs_cfg = {a["nombre"]: a for a in cfg["alycs"]}

    total_desc = total_sub = total_err = 0

    for nombre, fechas in FECHAS_POR_ALYC.items():
        alyc = alycs_cfg[nombre]
        logger.info("=" * 60)
        logger.info("[%s] %d fechas: %s → %s", nombre, len(fechas), fechas[0], fechas[-1])
        desc, sub, err = await process_alyc_batch(alyc, general, fechas, uploader)
        logger.info("[%s] desc=%d  sub=%d  err=%d", nombre, desc, sub, err)
        total_desc += desc
        total_sub += sub
        total_err += err

    logger.info("=" * 60)
    logger.info("TOTAL: desc=%d  sub=%d  err=%d", total_desc, total_sub, total_err)
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

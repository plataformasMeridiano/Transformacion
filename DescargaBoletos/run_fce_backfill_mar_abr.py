"""
run_fce_backfill_mar_abr.py — Descarga boletos Venta FCE-eCheq de Allaria, ADCAP y
Dhalmore para el rango marzo–abril 2026 (días hábiles).

Solo descarga el tipo "Venta FCE-eCheq" — sobreescribe tipo_operacion en cada config.
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

ALYCS_FCE   = {"Allaria", "ADCAP", "Dhalmore"}
TIPO_FCE     = ["Venta FCE-eCheq"]

INICIO = date(2026, 3, 1)
FIN    = date(2026, 4, 17)   # hoy inclusive


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = {**config["general"], "headless": True}

    fechas = business_days(INICIO, FIN)

    tag = f"fce_mar_abr_{INICIO.isoformat()}_a_{FIN.isoformat()}"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("fce_backfill")
    logger.info("=" * 60)
    logger.info("FCE backfill — %s a %s (%d días hábiles)", INICIO, FIN, len(fechas))
    logger.info("ALYCs: %s", sorted(ALYCS_FCE))
    logger.info("Fechas: %s", fechas)

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    alycs = [
        a for a in config["alycs"]
        if a.get("activo") and a["nombre"] in ALYCS_FCE
    ]
    logger.info("Entradas a procesar: %d", len(alycs))

    grand_desc = grand_sub = grand_err = 0
    for alyc_cfg in alycs:
        # Deep copy para no mutar el config original
        cfg = copy.deepcopy(alyc_cfg)
        cfg["opciones"]["tipo_operacion"] = TIPO_FCE
        logger.info("-" * 50)
        logger.info("[%s] tipo_operacion forzado a %s", cfg["nombre"], TIPO_FCE)

        desc, sub, err = await process_alyc_batch(cfg, general, fechas, uploader)
        grand_desc += desc
        grand_sub  += sub
        grand_err  += err

    logger.info("=" * 60)
    logger.info("RESUMEN FINAL: desc=%d  sub=%d  err=%d", grand_desc, grand_sub, grand_err)
    logger.info("=" * 60)
    return 0 if grand_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

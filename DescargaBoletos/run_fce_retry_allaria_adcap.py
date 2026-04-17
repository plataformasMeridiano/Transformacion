"""
run_fce_retry_allaria_adcap.py — Retry FCE backfill para:
  - Allaria: mar-abr completo (falló por headless en run anterior)
  - ADCAP:   solo 2026-04-16 y 2026-04-17 (timeout al final de sesión)
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

TIPO_FCE = ["Venta FCE-eCheq"]


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = config["general"]   # headless=false según config; cada scraper puede override

    tag = "fce_retry_allaria_adcap"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("fce_retry")

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    grand_desc = grand_sub = grand_err = 0

    # ── Allaria: mar-abr completo ─────────────────────────────────────────────
    fechas_allaria = business_days(date(2026, 3, 1), date(2026, 4, 17))
    logger.info("=" * 60)
    logger.info("Allaria FCE — %d fechas (mar-abr 2026)", len(fechas_allaria))

    allaria_cfgs = [a for a in config["alycs"] if a.get("activo") and a["nombre"] == "Allaria"]
    for alyc_cfg in allaria_cfgs:
        cfg = copy.deepcopy(alyc_cfg)
        cfg["opciones"]["tipo_operacion"] = TIPO_FCE
        desc, sub, err = await process_alyc_batch(cfg, general, fechas_allaria, uploader)
        grand_desc += desc; grand_sub += sub; grand_err += err

    # ── ADCAP: solo Apr 16-17 ────────────────────────────────────────────────
    fechas_adcap = ["2026-04-16", "2026-04-17"]
    logger.info("=" * 60)
    logger.info("ADCAP FCE retry — fechas: %s", fechas_adcap)

    adcap_cfgs = [a for a in config["alycs"] if a.get("activo") and a["nombre"] == "ADCAP"]
    for alyc_cfg in adcap_cfgs:
        cfg = copy.deepcopy(alyc_cfg)
        cfg["opciones"]["tipo_operacion"] = TIPO_FCE
        desc, sub, err = await process_alyc_batch(cfg, general, fechas_adcap, uploader)
        grand_desc += desc; grand_sub += sub; grand_err += err

    logger.info("=" * 60)
    logger.info("RESUMEN FINAL: desc=%d  sub=%d  err=%d", grand_desc, grand_sub, grand_err)
    return 0 if grand_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

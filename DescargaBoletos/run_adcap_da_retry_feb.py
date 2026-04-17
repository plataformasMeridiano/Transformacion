"""
run_adcap_da_retry_feb.py — Retry de fechas faltantes de ADCAP y DAValores (Pases feb 2026).

ADCAP:
  - 2026-02-11 (falta boleto Pamat 68891)
  - 2026-02-12 (faltan 72966 Pamat, 73222 MN, 73433 MN)

DA Valores:
  - 2026-02-23 a 2026-02-27 (faltan boletos 84893 de Pases)
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

    general = {**config["general"], "headless": True}

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    tag = "adcap_da_retry_feb2026"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger(tag)

    alycs_cfg = {a["nombre"]: a for a in config["alycs"] if a.get("activo")}

    runs = [
        ("ADCAP",     ["2026-02-12", "2026-02-11"]),
        ("DAValores", ["2026-02-27", "2026-02-26", "2026-02-25", "2026-02-24", "2026-02-23"]),
    ]

    total_desc = total_sub = total_err = 0
    for nombre, fechas in runs:
        alyc = alycs_cfg.get(nombre)
        if not alyc:
            logger.error("ALYC %s no encontrado en config", nombre)
            continue
        # Solo Pases para este retry
        import copy
        alyc = copy.deepcopy(alyc)
        alyc.setdefault("opciones", {})["tipo_operacion"] = ["Pases"]
        logger.info("=" * 60)
        logger.info("%s — %d fechas", nombre, len(fechas))
        desc, sub, err = await process_alyc_batch(alyc, general, fechas, uploader)
        logger.info("%s — desc=%d  sub=%d  err=%d", nombre, desc, sub, err)
        total_desc += desc; total_sub += sub; total_err += err

    logger.info("=" * 60)
    logger.info("TOTAL: desc=%d  sub=%d  err=%d", total_desc, total_sub, total_err)
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

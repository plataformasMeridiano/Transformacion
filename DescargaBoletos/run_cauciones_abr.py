"""
run_cauciones_abr.py — Descarga SOLO Cauciones para los días faltantes de abril 2026.
Fechas: 2026-04-02, 2026-04-03, 2026-04-04, 2026-04-06, 2026-04-07
"""
import asyncio
import copy
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from batch_download import _resolve_env, process_alyc_batch, setup_logging
from drive_uploader import DriveUploader

FECHAS = ["2026-04-02", "2026-04-03", "2026-04-04", "2026-04-06", "2026-04-07"]


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = {**config["general"], "headless": False}

    setup_logging(general["log_dir"], "cauciones_abr")
    logger = logging.getLogger("cauciones_abr")

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    alycs = [a for a in config["alycs"] if a.get("activo")]

    total_desc = total_sub = total_err = 0

    for alyc_cfg in alycs:
        # Override tipo_operacion a solo Cauciones
        alyc_override = copy.deepcopy(alyc_cfg)
        alyc_override.setdefault("opciones", {})["tipo_operacion"] = ["Cauciones"]

        logger.info("=" * 60)
        logger.info("%s — %d fechas", alyc_cfg["nombre"], len(FECHAS))
        desc, sub, err = await process_alyc_batch(alyc_override, general, FECHAS, uploader)
        logger.info("%s — desc=%d  sub=%d  err=%d", alyc_cfg["nombre"], desc, sub, err)
        total_desc += desc
        total_sub += sub
        total_err += err

    logger.info("=" * 60)
    logger.info("TOTAL FINAL: desc=%d  sub=%d  err=%d", total_desc, total_sub, total_err)
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
run_win_backfill.py — Backfill WIN febrero y marzo 2026 (más reciente primero).

Corrige la descarga de Pamat: antes el _switch_comitente se reseteaba con cada
page.goto() en el loop de tipos, así que Pamat siempre bajaba los boletos de
MeridianoNorte. Con el fix en alyc_sistemaC.py ya funciona correctamente.
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

    inicio = date(2026, 2, 2)
    fin    = date(2026, 3, 26)
    fechas = list(reversed(business_days(inicio, fin)))  # más reciente primero

    tag = f"win_backfill_{inicio.isoformat()}_a_{fin.isoformat()}"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("win_backfill")
    logger.info("=" * 60)
    logger.info("WIN backfill — %s a %s (%d días hábiles)", fin, inicio, len(fechas))

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    alycs = [a for a in config["alycs"] if a.get("activo") and a["nombre"] == "WIN"]
    if not alycs:
        logger.error("No se encontró WIN activo en config.json")
        return 1

    desc, sub, err = await process_alyc_batch(alycs[0], general, fechas, uploader)

    logger.info("=" * 60)
    logger.info("RESUMEN FINAL: desc=%d  sub=%d  err=%d", desc, sub, err)
    logger.info("=" * 60)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

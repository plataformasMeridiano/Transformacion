"""
run_conosur_fix_retry_mn.py — Redownload ConoSur MN (cuenta 3003) para el
rango feb-19 a mar-12 que falló por expiración de JWT en el run anterior.

Solo corre la cuenta 3003 (Meridiano Norte).  La cuenta 3087 (Pamat) completó
sin errores en el run anterior y no necesita reintento.
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

    inicio = date(2026, 2, 19)
    fin    = date(2026, 3, 12)
    fechas = business_days(inicio, fin)

    tag = f"conosur_mn_retry_{inicio.isoformat()}_a_{fin.isoformat()}"
    setup_logging(general["log_dir"], tag)
    logger = logging.getLogger("conosur_mn_retry")
    logger.info("=" * 60)
    logger.info("ConoSur MN retry — %s a %s (%d días hábiles)", inicio, fin, len(fechas))

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    # Solo cuenta 3003 (MN)
    alycs = [
        a for a in config["alycs"]
        if a.get("activo") and a["nombre"] == "ConoSur"
        and a["opciones"]["cuenta"] == "3003"
    ]
    if not alycs:
        logger.error("No se encontró la entrada ConoSur cuenta=3003 en config.json")
        return 1
    logger.info("Cuenta ConoSur MN: %d entrada(s) — cuenta=%s",
                len(alycs), alycs[0]["opciones"]["cuenta"])

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

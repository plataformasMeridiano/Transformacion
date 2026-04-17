"""
cleanup_redescargar_feb.py

Elimina de Drive y locales los boletos específicos a redescargar.
Luego los scrapers los vuelven a bajar desde cero.
"""
import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_FOLDER_MIME = "application/vnd.google-apps.folder"

# (tipo, fecha_iso, alyc_drive_name, alyc_local_name, nro_boleto)
TARGETS = [
    # ADCAP
    ("Cauciones", "2026-02-03", "ADCAP", "ADCAP", "56403"),
    ("Cauciones", "2026-02-03", "ADCAP", "ADCAP", "56404"),
    ("Cauciones", "2026-02-20", "ADCAP", "ADCAP", "83992"),
    # Dhalmore
    ("Cauciones", "2026-02-02", "Dhalmore", "Dhalmore", "7445"),
    ("Cauciones", "2026-02-05", "Dhalmore", "Dhalmore", "8445"),
    # MaxCapital
    ("Cauciones", "2026-02-09", "MaxCapital", "MaxCapital", "17322"),
    # MetroCorp
    ("Cauciones", "2026-02-04", "MetroCorp", "MetroCorp", "3556"),
    ("Cauciones", "2026-02-06", "MetroCorp", "MetroCorp", "3885"),
    ("Cauciones", "2026-02-10", "MetroCorp", "MetroCorp", "4260"),
    # Puente
    ("Cauciones", "2026-02-10", "Puente", "Puente", "12170"),
    ("Cauciones", "2026-02-10", "Puente", "Puente", "12187"),
    # WIN
    ("Cauciones", "2026-02-13", "WIN", "WIN", "6639"),
    ("Cauciones", "2026-02-13", "WIN", "WIN", "6640"),
]


def find_folder(svc, name, parent_id):
    q = (f"name = '{name}' and mimeType = '{_FOLDER_MIME}'"
         f" and '{parent_id}' in parents and trashed = false")
    r = svc.files().list(q=q, fields="files(id)", pageSize=5,
                         includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
    f = r.get("files", [])
    return f[0]["id"] if f else None


def find_file(svc, name, folder_id):
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    r = svc.files().list(q=q, fields="files(id,name)", pageSize=5,
                         includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
    f = r.get("files", [])
    return f[0] if f else None


def main():
    dry_run = "--dry-run" in sys.argv

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)
    gd = config["google_drive"]
    root_folder_id = re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], gd["root_folder_id"])
    creds = service_account.Credentials.from_service_account_file(
        gd["credentials_file"], scopes=_SCOPES)
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    download_dir = Path(config["general"]["download_dir"])
    folder_cache = {}

    trashed = not_found_drive = deleted_local = 0

    for tipo, fecha, alyc_drive, alyc_local, nro in TARGETS:
        drive_name = f"Boleto - {alyc_drive} - {nro}.pdf"

        # ── Drive ─────────────────────────────────────────────────────────────
        tipo_id = folder_cache.get(tipo)
        if not tipo_id:
            tipo_id = find_folder(svc, tipo, root_folder_id)
            folder_cache[tipo] = tipo_id

        fecha_id = folder_cache.get((tipo, fecha))
        if not fecha_id:
            fecha_id = find_folder(svc, fecha, tipo_id) if tipo_id else None
            folder_cache[(tipo, fecha)] = fecha_id

        if fecha_id:
            f = find_file(svc, drive_name, fecha_id)
            if f:
                logger.info("TRASH Drive: %s/%s/%s", tipo, fecha, drive_name)
                if not dry_run:
                    svc.files().update(fileId=f["id"], body={"trashed": True},
                                       fields="id", supportsAllDrives=True).execute()
                trashed += 1
            else:
                logger.warning("NOT FOUND Drive: %s/%s/%s", tipo, fecha, drive_name)
                not_found_drive += 1
        else:
            logger.warning("NOT FOUND carpeta Drive: %s/%s", tipo, fecha)
            not_found_drive += 1

        # ── Local ─────────────────────────────────────────────────────────────
        # Buscar en downloads/{alyc_local}/{fecha}/**/  (con o sin subcarpeta de cuenta)
        pattern = f"{nro}.pdf"
        for local_file in (download_dir / alyc_local / fecha).rglob(pattern):
            logger.info("DELETE local: %s", local_file)
            if not dry_run:
                local_file.unlink()
            deleted_local += 1
        # Borrar marker si existe (idMovimiento.pdf con contenido = nro)
        for marker in (download_dir / alyc_local / fecha).rglob("*.pdf"):
            if marker.stat().st_size < 100:
                try:
                    if marker.read_text().strip() == nro:
                        logger.info("DELETE marker: %s", marker)
                        if not dry_run:
                            marker.unlink()
                        deleted_local += 1
                except Exception:
                    pass

    logger.info("=" * 60)
    logger.info("Drive trashed=%d  not_found=%d  | Local deleted=%d",
                trashed, not_found_drive, deleted_local)


if __name__ == "__main__":
    main()

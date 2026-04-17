"""
cleanup_puente_pases_20mar.py

Mueve a papelera en Drive todos los boletos de Pases/2026-03-20/Puente/
EXCEPTO los 4 boletos correctos.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_FOLDER_MIME = "application/vnd.google-apps.folder"

FECHA = "2026-03-20"
KEEP = {
    "Boleto - Puente - 23780.pdf",
    "Boleto - Puente - 23781.pdf",
    "Boleto - Puente - 364541.pdf",
    "Boleto - Puente - 59224.pdf",
}


def find_folder(svc, name: str, parent_id: str) -> str | None:
    q = (f"name = '{name}' and mimeType = '{_FOLDER_MIME}'"
         f" and '{parent_id}' in parents and trashed = false")
    r = svc.files().list(q=q, fields="files(id)", pageSize=5,
                         includeItemsFromAllDrives=True,
                         supportsAllDrives=True).execute()
    files = r.get("files", [])
    return files[0]["id"] if files else None


def list_files(svc, folder_id: str) -> list[dict]:
    results = []
    page_token = None
    while True:
        r = svc.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name)",
            pageSize=200,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageToken=page_token,
        ).execute()
        results.extend(r.get("files", []))
        page_token = r.get("nextPageToken")
        if not page_token:
            break
    return results


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("=== DRY RUN ===")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    gd = config["google_drive"]
    root_folder_id = re.sub(
        r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], gd["root_folder_id"]
    )
    creds = service_account.Credentials.from_service_account_file(
        gd["credentials_file"], scopes=_SCOPES
    )
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    # Navigate: root → Pases → 2026-03-20 (files are flat, no Puente subfolder)
    pases_id = find_folder(svc, "Pases", root_folder_id)
    if not pases_id:
        logger.error("Carpeta 'Pases' no encontrada en root")
        return 1

    fecha_id = find_folder(svc, FECHA, pases_id)
    if not fecha_id:
        logger.error("Carpeta '%s' no encontrada en Pases", FECHA)
        return 1

    all_files = list_files(svc, fecha_id)
    logger.info("Archivos totales en Pases/%s: %d", FECHA, len(all_files))

    # Only touch Puente files; leave other ALYCs untouched
    puente_files = [f for f in all_files if f["name"].startswith("Boleto - Puente - ")]
    logger.info("Archivos Puente: %d", len(puente_files))

    to_trash = [f for f in puente_files if f["name"] not in KEEP]
    to_keep  = [f for f in puente_files if f["name"] in KEEP]

    logger.info("Conservar: %d  |  Enviar a papelera: %d", len(to_keep), len(to_trash))
    for f in to_keep:
        logger.info("  KEEP  %s", f["name"])

    trashed = errors = 0
    for f in to_trash:
        logger.info("  TRASH  %s  (id=%s)", f["name"], f["id"])
        if not dry_run:
            try:
                svc.files().update(
                    fileId=f["id"],
                    body={"trashed": True},
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
                trashed += 1
            except Exception as exc:
                logger.error("  ERROR id=%s: %s", f["id"], exc)
                errors += 1
        else:
            trashed += 1

    logger.info("=" * 60)
    logger.info("TOTAL: en_papelera=%d  errores=%d", trashed, errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

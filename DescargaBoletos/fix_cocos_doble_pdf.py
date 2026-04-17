"""
fix_cocos_doble_pdf.py

Busca archivos en Drive cuyo nombre termine en ".pdf.pdf" y los renombra
quitando la extensión duplicada (ej: "Boleto - Cocos - 123.pdf.pdf" → "Boleto - Cocos - 123.pdf").

Sólo recorre las carpetas de Cocos bajo Cauciones y Pases.
"""
import json
import logging
import os
import re
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


def resolve_env(v: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], v)


def list_all_files(svc, folder_id: str) -> list[dict]:
    """Lista recursivamente todos los archivos bajo folder_id."""
    results = []
    page_token = None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                results.extend(list_all_files(svc, f["id"]))
            else:
                results.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def main():
    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    gd = config["google_drive"]
    root_folder_id = resolve_env(gd["root_folder_id"])

    creds = service_account.Credentials.from_service_account_file(
        gd["credentials_file"], scopes=_SCOPES
    )
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    logger.info("Buscando archivos con nombre .pdf.pdf bajo el root de Drive...")
    all_files = list_all_files(svc, root_folder_id)

    # También buscar bajo el override folder de Venta FCE-eCheq por si acaso
    override_ids = list(gd.get("tipo_folder_overrides", {}).values())
    for oid in override_ids:
        all_files.extend(list_all_files(svc, oid))

    # Filtrar los que tienen doble extensión
    doble_pdf = [f for f in all_files if f["name"].endswith(".pdf.pdf")]
    logger.info("Archivos con doble .pdf encontrados: %d", len(doble_pdf))

    if not doble_pdf:
        logger.info("Nada que corregir.")
        return 0

    fixed = errors = 0
    for f in doble_pdf:
        old_name = f["name"]
        new_name = old_name[:-4]  # quitar el último ".pdf"
        logger.info("  %s  →  %s  (id=%s)", old_name, new_name, f["id"])
        try:
            svc.files().update(
                fileId=f["id"],
                body={"name": new_name},
                fields="id, name",
                supportsAllDrives=True,
            ).execute()
            fixed += 1
        except Exception as exc:
            logger.error("  ERROR renombrando %s: %s", old_name, exc)
            errors += 1

    logger.info("=" * 50)
    logger.info("TOTAL: renombrados=%d  errores=%d", fixed, errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

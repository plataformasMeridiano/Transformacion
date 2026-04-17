"""
cleanup_puente_nombres_drive.py

Busca y elimina de Drive TODOS los archivos de Puente con nombre incorrecto
(idMovimiento en lugar de nro boleto): "Boleto - Puente - 16xxxxxx.pdf"

Hace un listado completo primero y luego borra, con paginación correcta.
"""
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

with open("config.json") as f:
    cfg = json.load(f)

gd = cfg["google_drive"]
root_folder_id = re.sub(
    r'\$\{(\w+)\}',
    lambda m: __import__('os').environ[m.group(1)],
    gd["root_folder_id"]
)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_RE_NOMBRE_MALO = re.compile(r"^1[5-9]\d{5,}$")

creds = service_account.Credentials.from_service_account_file(gd["credentials_file"], scopes=_SCOPES)
svc = build("drive", "v3", credentials=creds, cache_discovery=False)


def _nombre_malo(filename: str) -> bool:
    stem = Path(filename).stem           # "Boleto - Puente - 16524328"
    parts = stem.split(" - ")
    nro = parts[-1] if parts else ""
    return bool(_RE_NOMBRE_MALO.match(nro))


def list_all_puente_bad_files():
    """Lista todos los PDFs de Puente con nombre incorrecto en todo Drive."""
    bad = []
    page_token = None
    while True:
        resp = svc.files().list(
            q=(f"name contains 'Puente'"
               f" and mimeType = 'application/pdf'"
               f" and trashed = false"),
            fields="files(id, name, parents), nextPageToken",
            pageSize=1000,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            corpora="allDrives",
            pageToken=page_token,
        ).execute()

        for f in resp.get("files", []):
            if _nombre_malo(f["name"]):
                bad.append(f)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return bad


dry_run = "--dry-run" in sys.argv

print("Buscando archivos de Puente con nombre incorrecto en Drive...")
bad_files = list_all_puente_bad_files()
print(f"Encontrados: {len(bad_files)}")

deleted = 0
skipped = 0
for f in bad_files:
    print(f"  {'[DRY]' if dry_run else 'DELETE'}: {f['name']}  (id={f['id']})")
    if not dry_run:
        try:
            svc.files().update(
                fileId=f["id"],
                body={"trashed": True},
                supportsAllDrives=True,
            ).execute()
            deleted += 1
        except Exception as e:
            print(f"    SKIP ({e})")
            skipped += 1

print(f"\n{'Simulación' if dry_run else 'Borrado'} completo: {len(bad_files)} archivos encontrados, {deleted} borrados, {skipped} ya no existían.")
if dry_run:
    print("Corré sin --dry-run para borrar realmente.")

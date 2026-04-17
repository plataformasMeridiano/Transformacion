"""
cleanup_metro_wrong_pdfs.py

Detecta PDFs de MetroCorp descargados incorrectamente (extractos de "Movimientos"
y "Garantía Caucion Titulos") usando pdfplumber, y los mueve a la papelera en Drive.

Criterios de descarte:
  - El texto de la primera página empieza con "Movimientos" → extracto de cuenta
  - El texto contiene "GARANTIA CAUCION TITULOS" → garantía en títulos, no caución

Uso:
    python3 cleanup_metro_wrong_pdfs.py [--dry-run]
"""
import json
import logging
import os
import re
import sys
from pathlib import Path

import pdfplumber
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
_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_wrong_pdf(pdf_path: Path) -> tuple[bool, str]:
    """
    Retorna (es_incorrecto, motivo).
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = (pdf.pages[0].extract_text() or "").strip()
    except Exception as e:
        return False, f"error_lectura: {e}"

    if text.startswith("Movimientos"):
        return True, "extracto_movimientos"
    if "GARANTIA CAUCION TITULOS" in text.upper():
        return True, "garantia_caucion_titulos"
    return False, ""


def find_drive_file(svc, root_folder_id: str, fecha: str, nro: str) -> str | None:
    """
    Busca en Drive: Cauciones/{fecha}/Boleto - MetroCorp - {nro}.pdf
    Retorna el file_id o None.
    """
    _FOLDER_MIME = "application/vnd.google-apps.folder"

    def find_folder(name, parent_id):
        q = (f"name = '{name}' and mimeType = '{_FOLDER_MIME}'"
             f" and '{parent_id}' in parents and trashed = false")
        r = svc.files().list(q=q, fields="files(id)", pageSize=5,
                             includeItemsFromAllDrives=True,
                             supportsAllDrives=True).execute()
        files = r.get("files", [])
        return files[0]["id"] if files else None

    cauciones_id = find_folder("Cauciones", root_folder_id)
    if not cauciones_id:
        return None
    fecha_id = find_folder(fecha, cauciones_id)
    if not fecha_id:
        return None

    dest_name = f"Boleto - MetroCorp - {nro}.pdf"
    safe = dest_name.replace("'", "\\'")
    q = (f"name = '{safe}' and '{fecha_id}' in parents"
         f" and mimeType = 'application/pdf' and trashed = false")
    r = svc.files().list(q=q, fields="files(id, name)", pageSize=5,
                         includeItemsFromAllDrives=True,
                         supportsAllDrives=True).execute()
    files = r.get("files", [])
    return files[0]["id"] if files else None


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("=== DRY RUN — no se moverá nada a papelera ===")

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

    metro_dir = Path(__file__).parent / "downloads" / "MetroCorp"
    if not metro_dir.exists():
        logger.error("Carpeta no encontrada: %s", metro_dir)
        return 1

    # Recolectar todos los PDFs de Cauciones de MetroCorp
    wrong: list[tuple[Path, str, str, str]] = []  # (path, fecha, nro, motivo)

    for fecha_dir in sorted(metro_dir.iterdir()):
        if not fecha_dir.is_dir() or not _RE_FECHA.match(fecha_dir.name):
            continue
        fecha = fecha_dir.name
        # Estructura: {fecha}/{cuenta}/Cauciones/*.pdf  o  {fecha}/Cauciones/*.pdf
        for cauc_dir in fecha_dir.rglob("Cauciones"):
            if not cauc_dir.is_dir():
                continue
            for pdf in sorted(cauc_dir.glob("*.pdf")):
                bad, motivo = is_wrong_pdf(pdf)
                if bad:
                    nro = pdf.stem
                    wrong.append((pdf, fecha, nro, motivo))
                    logger.info("WRONG  %s/%s  (%s)", fecha, pdf.name, motivo)

    logger.info("=" * 60)
    logger.info("PDFs incorrectos encontrados: %d", len(wrong))

    if not wrong:
        logger.info("Nada que limpiar.")
        return 0

    trashed = errors = not_found = 0

    for pdf_path, fecha, nro, motivo in wrong:
        drive_id = find_drive_file(svc, root_folder_id, fecha, nro)
        if not drive_id:
            logger.warning("  NO ENCONTRADO en Drive: Cauciones/%s/Boleto - MetroCorp - %s.pdf", fecha, nro)
            not_found += 1
            continue

        logger.info("  TRASH  Cauciones/%s/Boleto - MetroCorp - %s.pdf  (id=%s)  [%s]",
                    fecha, nro, drive_id, motivo)
        if not dry_run:
            try:
                svc.files().update(
                    fileId=drive_id,
                    body={"trashed": True},
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
                trashed += 1
            except Exception as exc:
                logger.error("  ERROR trashing id=%s: %s", drive_id, exc)
                errors += 1
        else:
            trashed += 1

    logger.info("=" * 60)
    logger.info("TOTAL: en_papelera=%d  no_en_drive=%d  errores=%d", trashed, not_found, errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

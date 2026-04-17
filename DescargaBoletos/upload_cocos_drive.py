"""
upload_cocos_drive.py

Lee los boletos de Cocos Capital desde la carpeta fuente de Drive
(estructura: fecha_entrega/ Boleto_{ID}_{comitente}_{DDMMYYYY}.pdf.pdf),
extrae el número de boleto y el tipo desde el PDF, y los sube a la
estructura principal de Drive (Cauciones|Pases / fecha_op / Boleto - Cocos - {nro}.pdf).

Uso:
    python3 upload_cocos_drive.py [--dry-run] [--desde YYYY-MM-DD]

    --dry-run   Lista lo que haría sin subir nada
    --desde     Solo procesa carpetas con fecha_entrega >= YYYY-MM-DD (default: hoy-7d)
"""
import io
import json
import logging
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from drive_uploader import DriveUploader
from supabase_logger import log_boleto

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cocos_drive")

_SCOPES       = ["https://www.googleapis.com/auth/drive"]
_SOURCE_FOLDER = "1xkOuACdcA2UbmUj6vIEaYqEGD6DuMxRS"
_FOLDER_MIME  = "application/vnd.google-apps.folder"
_ALYC         = "Cocos"

# Extrae fecha de operación del nombre de archivo:
# Boleto_{ID}_{comitente}_{DDMMYYYY}.pdf.pdf  →  DDMMYYYY
_RE_FNAME_DATE = re.compile(r"_(\d{2})(\d{2})(\d{4})\.pdf", re.IGNORECASE)

# Extrae número de boleto del texto del PDF:
# Línea: {comitente} {YYYY-MM-DD} {YYYY-MM-DD} {nro}
_RE_NRO = re.compile(
    r"\d+\s+"                   # comitente
    r"\d{4}-\d{2}-\d{2}\s+"     # fecha operación
    r"\d{4}-\d{2}-\d{2}\s+"     # fecha liquidación
    r"(\d+)"                    # número de boleto ← captura
)


def build_drive_service(creds_file: str):
    creds = service_account.Credentials.from_service_account_file(
        creds_file, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folder(svc, folder_id: str) -> list[dict]:
    """Lista todos los archivos (no carpetas) de un folder, manejando paginación."""
    items = []
    page_token = None
    while True:
        r = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType)",
            pageSize=100,
            pageToken=page_token,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        items.extend(r.get("files", []))
        page_token = r.get("nextPageToken")
        if not page_token:
            break
    return items


def download_file(svc, file_id: str) -> bytes:
    buf = io.BytesIO()
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    dl  = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf.read()


def extract_info(pdf_bytes: bytes) -> tuple[str | None, str]:
    """
    Extrae (nro_boleto, tipo_operacion) del texto del PDF.
    tipo_operacion: "Cauciones" si el texto contiene "aución", "Pases" en caso contrario.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = _RE_NRO.search(text)
            if m:
                nro = m.group(1)
                tipo = "Cauciones" if re.search(r"au[cç]i[oó]n", text, re.IGNORECASE) else "Pases"
                return nro, tipo
    return None, "Pases"


def fecha_from_filename(name: str) -> str | None:
    """Extrae la fecha de operación del nombre de archivo (DDMMYYYY → YYYY-MM-DD)."""
    m = _RE_FNAME_DATE.search(name)
    if not m:
        return None
    dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
    try:
        fecha = date(int(yyyy), int(mm), int(dd))
        return fecha.isoformat()
    except ValueError:
        return None


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    desde_str = None
    if "--desde" in sys.argv:
        idx = sys.argv.index("--desde")
        desde_str = sys.argv[idx + 1]
    desde = date.fromisoformat(desde_str) if desde_str else date.today() - timedelta(days=7)

    with open(Path(__file__).parent / "config.json") as f:
        cfg = json.load(f)
    gd  = cfg["google_drive"]
    root_folder_id = re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], gd["root_folder_id"])

    svc = build_drive_service(gd["credentials_file"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    # Listar subcarpetas de fecha en el folder fuente
    all_items = list_folder(svc, _SOURCE_FOLDER)
    date_folders = sorted(
        [f for f in all_items if _FOLDER_MIME in f["mimeType"]
         and re.match(r"\d{4}-\d{2}-\d{2}", f["name"])
         and date.fromisoformat(f["name"]) >= desde],
        key=lambda f: f["name"],
    )
    logger.info("Carpetas de entrega >= %s: %d", desde, len(date_folders))

    total = subidos = skipped = errores = 0

    for folder in date_folders:
        folder_date = folder["name"]
        files = [f for f in list_folder(svc, folder["id"])
                 if not _FOLDER_MIME in f["mimeType"]
                 and f["name"].lower().endswith(".pdf")]

        logger.info("%s — %d PDFs", folder_date, len(files))

        for f in files:
            total += 1
            fname = f["name"]

            # Fecha de operación desde el nombre del archivo
            fecha = fecha_from_filename(fname)
            if not fecha:
                logger.warning("  Sin fecha en nombre: %s — usando fecha carpeta", fname)
                fecha = folder_date

            if dry_run:
                logger.info("  [DRY] %s  fecha=%s", fname, fecha)
                continue

            try:
                pdf_bytes = download_file(svc, f["id"])
            except Exception as exc:
                logger.error("  Descarga fallida: %s — %s", fname, exc)
                errores += 1
                continue

            nro, tipo = extract_info(pdf_bytes)
            if not nro:
                logger.warning("  Sin nro boleto en PDF: %s", fname)
                errores += 1
                continue

            logger.info("  %s  fecha=%s  tipo=%s  nro=%s", fname, fecha, tipo, nro)

            # Guardar temp y subir
            tmp = Path(f"/tmp/cocos_{nro}.pdf")
            try:
                tmp.write_bytes(pdf_bytes)
                file_id = uploader.upload_boleto(
                    pdf_path=tmp,
                    tipo_operacion=tipo,
                    fecha=fecha,
                    alyc_nombre=_ALYC,
                    nro_boleto=nro,
                )
                log_boleto(fecha, _ALYC, tipo, nro, f"Boleto - {_ALYC} - {nro}.pdf", file_id)
                logger.info("  ✓ Drive  %s/%s/Boleto - Cocos - %s.pdf  (id=%s)",
                            tipo, fecha, nro, file_id)
                subidos += 1
            except Exception as exc:
                logger.error("  Drive falló: %s — %s", fname, exc)
                errores += 1
            finally:
                tmp.unlink(missing_ok=True)

    logger.info("=" * 60)
    if dry_run:
        logger.info("DRY RUN — total=%d", total)
    else:
        logger.info("TOTAL: procesados=%d  subidos=%d  errores=%d", total, subidos, errores)
    return 0 if errores == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

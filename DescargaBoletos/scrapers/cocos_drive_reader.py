"""
cocos_drive_reader.py — Lee boletos de Cocos Capital desde el Drive de descargas brutas.

Cocos no tiene portal scrapeado: los boletos llegan por email y un Zap los copia
a una carpeta Drive organizada por fecha (YYYY-MM-DD/).

Flujo:
    1. Lista archivos en {COCOS_RAW_FOLDER}/{fecha}/
    2. Descarga cada PDF y extrae: número de boleto, fecha operación, tipo
    3. Guarda en downloads/Cocos/{fecha}/{tipo}/{nro}.pdf

Integración:
    - Se usa desde batch_download.py igual que un scraper normal
    - No requiere Playwright ni credenciales web
"""
import io
import logging
import os
import re
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

_CREDS_FILE      = Path(__file__).parent.parent / "credentials" / "gdrive_service_account.json"
_RAW_FOLDER_ID   = os.environ.get("COCOS_RAW_FOLDER_ID", "1xkOuACdcA2UbmUj6vIEaYqEGD6DuMxRS")
_FOLDER_MIME     = "application/vnd.google-apps.folder"

# Regex: línea con comitente fecha_op fecha_liq numero
# Ej: "1555 2026-04-17 2026-04-20 3040388"
_RE_INFO = re.compile(
    r"(\d{3,6})\s+"            # comitente (3-6 dígitos)
    r"(\d{4}-\d{2}-\d{2})\s+"  # fecha operación
    r"(\d{4}-\d{2}-\d{2})\s+"  # fecha liquidación
    r"(\d{4,})"                 # número de boleto (≥4 dígitos)
)
_RE_OPERACION = re.compile(r"Operaci[oó]n:\s*(.+)", re.IGNORECASE)


def _build_service():
    creds = service_account.Credentials.from_service_account_file(
        str(_CREDS_FILE), scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _list_folder(svc, parent_id: str) -> list[dict]:
    items, token = [], None
    while True:
        r = svc.files().list(
            q=f"'{parent_id}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType)",
            pageSize=100, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        items.extend(r.get("files", []))
        token = r.get("nextPageToken")
        if not token:
            break
    return items


def _download_bytes(svc, file_id: str) -> bytes:
    buf = io.BytesIO()
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def _classify_tipo(operacion: str) -> str:
    op = operacion.lower()
    if "cauci" in op:
        return "Cauciones"
    if "pase" in op:
        return "Pases"
    # SENEBI = operaciones bilaterales (incluye pases OTC)
    if "senebi" in op:
        return "Pases"
    # Otros: Venta, Compra directa, etc. → Pases por defecto
    return "Pases"


def _extract_info(pdf_bytes: bytes, filename: str) -> dict | None:
    """
    Extrae número de boleto, fecha de operación y tipo desde el PDF.
    Retorna dict con {nro, fecha, tipo} o None si no pudo parsear.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        logger.warning("Error leyendo PDF %s: %s", filename, e)
        return None

    m_info = _RE_INFO.search(text)
    if not m_info:
        logger.warning("No se encontró línea de info en %s", filename)
        return None

    nro        = m_info.group(4)
    fecha      = m_info.group(2)   # fecha de operación

    m_op = _RE_OPERACION.search(text)
    operacion  = m_op.group(1).strip() if m_op else ""
    tipo       = _classify_tipo(operacion)

    logger.debug("  %s → nro=%s fecha=%s tipo=%s op='%s'", filename, nro, fecha, tipo, operacion)
    return {"nro": nro, "fecha": fecha, "tipo": tipo, "operacion": operacion}


class CocosReader:
    """
    Lee boletos de Cocos Capital desde el Drive de descargas brutas.
    Interfaz compatible con los scrapers: download_tickets(fecha, dest_dir).
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        self.nombre = alyc_config.get("nombre", "Cocos")
        self._svc = None

    async def __aenter__(self):
        self._svc = _build_service()
        return self

    async def __aexit__(self, *_):
        pass

    async def login(self) -> bool:
        return True   # no hay login; acceso vía service account

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga los boletos de Cocos para `fecha` (YYYY-MM-DD).
        Los archivos se guardan en dest_dir/{tipo}/{nro}.pdf
        usando la fecha de operación del PDF (puede diferir de `fecha`).
        """
        svc = self._svc

        # Buscar carpeta de la fecha en el raw folder
        top = _list_folder(svc, _RAW_FOLDER_ID)
        fecha_folder = next(
            (f for f in top if f["name"] == fecha and f["mimeType"] == _FOLDER_MIME),
            None,
        )
        if not fecha_folder:
            logger.info("[%s] No hay carpeta para %s en Drive raw", self.nombre, fecha)
            return []

        raw_files = [
            f for f in _list_folder(svc, fecha_folder["id"])
            if f["mimeType"] != _FOLDER_MIME
        ]
        logger.info("[%s] %s: %d archivos en Drive raw", self.nombre, fecha, len(raw_files))

        downloaded: list[Path] = []

        for f in raw_files:
            filename = f["name"]
            logger.info("[%s] Procesando %s", self.nombre, filename)

            pdf_bytes = _download_bytes(svc, f["id"])
            info = _extract_info(pdf_bytes, filename)
            if not info:
                continue

            nro   = info["nro"]
            fecha_op = info["fecha"]   # usa la fecha del PDF, no la carpeta
            tipo  = info["tipo"]

            # Guardar bajo la fecha de operación del PDF
            tipo_dir = dest_dir.parent / fecha_op / tipo
            tipo_dir.mkdir(parents=True, exist_ok=True)
            dest_path = tipo_dir / f"{nro}.pdf"

            if dest_path.exists():
                logger.debug("[%s] Ya existe: %s", self.nombre, dest_path)
                downloaded.append(dest_path)
                continue

            dest_path.write_bytes(pdf_bytes)
            logger.info("[%s] Guardado: %s (%d bytes)", self.nombre, dest_path, len(pdf_bytes))
            downloaded.append(dest_path)

        return downloaded

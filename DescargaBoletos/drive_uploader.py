import logging
import re
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_FOLDER_MIME = "application/vnd.google-apps.folder"


def nro_from_filename(filename: str) -> str:
    """
    Extrae el número de boleto del nombre de archivo descargado.
    Si hay varios grupos de dígitos, devuelve el más largo (único identificador).
    Ejemplos:
        "BOLETO_NRO_38288.pdf"      → "38288"
        "11454.pdf"                 → "11454"   (WIN: N° Ope. directo)
        "BOL_2026077209.pdf"        → "2026077209"
    """
    matches = re.findall(r"\d+", Path(filename).stem)
    if not matches:
        return Path(filename).stem
    return max(matches, key=len)


class DriveUploader:
    """
    Sube boletos PDF a Google Drive bajo la estructura:

        root_folder (Boletos/)
        └── tipo_operacion/   (ej: Cauciones, Pases)
            └── YYYY-MM-DD/
                └── Boleto - {ALYC} - {NRO}.pdf

    Las carpetas intermedias se crean solo si no existen.
    Usa un caché en memoria para evitar llamadas redundantes a la API
    durante la misma sesión.
    """

    def __init__(
        self,
        credentials_file: str,
        root_folder_id: str,
        tipo_folder_overrides: dict[str, str] | None = None,
    ):
        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=_SCOPES
        )
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._root_id = root_folder_id
        self._folder_cache: dict[tuple[str, str], str] = {}
        # Mapping tipo_operacion → folder_id raíz alternativo.
        # Cuando un tipo está en este dict, los archivos van directo a
        # override_folder / fecha / archivo (sin nivel tipo_operacion bajo root).
        self._tipo_overrides: dict[str, str] = tipo_folder_overrides or {}

    # ── Carpetas ──────────────────────────────────────────────────────────

    def _get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Retorna el ID de la carpeta 'name' bajo 'parent_id', creándola si no existe."""
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]

        safe_name = name.replace("'", "\\'")
        query = (
            f"name = '{safe_name}'"
            f" and mimeType = '{_FOLDER_MIME}'"
            f" and '{parent_id}' in parents"
            f" and trashed = false"
        )
        results = (
            self._svc.files()
            .list(
                q=query,
                fields="files(id, name)",
                spaces="drive",
                pageSize=10,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])

        if files:
            folder_id = files[0]["id"]
            logger.debug("Carpeta existente: '%s' (id=%s)", name, folder_id)
        else:
            metadata = {
                "name": name,
                "mimeType": _FOLDER_MIME,
                "parents": [parent_id],
            }
            folder = (
                self._svc.files()
                .create(body=metadata, fields="id", supportsAllDrives=True)
                .execute()
            )
            folder_id = folder["id"]
            logger.info("Carpeta creada en Drive: '%s' (id=%s)", name, folder_id)

        self._folder_cache[key] = folder_id
        return folder_id

    # ── Subida ────────────────────────────────────────────────────────────

    def upload_boleto(
        self,
        pdf_path: Path,
        tipo_operacion: str,
        fecha: str,
        alyc_nombre: str,
        nro_boleto: str,
        overwrite: bool = False,
    ) -> str:
        """
        Sube un boleto PDF a Drive.

        Args:
            pdf_path:       Ruta local al PDF descargado.
            tipo_operacion: Nombre legible del tipo (ej: "Cauciones", "Pases").
            fecha:          Fecha en formato YYYY-MM-DD (nombre de la subcarpeta).
            alyc_nombre:    Nombre de la ALYC tal como figura en config.json.
            nro_boleto:     Número del boleto (solo dígitos, ej: "38288").
            overwrite:      Si True y el archivo ya existe, reemplaza su contenido.

        Returns:
            ID del archivo en Google Drive.
        """
        if tipo_operacion in self._tipo_overrides:
            # Carpeta raíz alternativa: override_root / fecha / archivo
            fecha_id = self._get_or_create_folder(fecha, self._tipo_overrides[tipo_operacion])
        else:
            tipo_id  = self._get_or_create_folder(tipo_operacion, self._root_id)
            fecha_id = self._get_or_create_folder(fecha, tipo_id)

        dest_name = f"Boleto - {alyc_nombre} - {nro_boleto}.pdf"

        # Verificar si ya existe un archivo con el mismo nombre en la carpeta destino
        safe_name = dest_name.replace("'", "\\'")
        existing = (
            self._svc.files()
            .list(
                q=(
                    f"name = '{safe_name}'"
                    f" and '{fecha_id}' in parents"
                    f" and mimeType = 'application/pdf'"
                    f" and trashed = false"
                ),
                fields="files(id, name)",
                pageSize=5,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
            .get("files", [])
        )
        if existing:
            if not overwrite:
                logger.info(
                    "Drive SKIP (ya existe): %s/%s/%s  (id=%s)",
                    tipo_operacion, fecha, dest_name, existing[0]["id"],
                )
                return existing[0]["id"]
            # Overwrite: actualizar contenido del archivo existente
            media = MediaFileUpload(str(pdf_path), mimetype="application/pdf", resumable=False)
            file_ = (
                self._svc.files()
                .update(
                    fileId=existing[0]["id"],
                    media_body=media,
                    fields="id, name",
                    supportsAllDrives=True,
                )
                .execute()
            )
            logger.info(
                "Drive update OK: %s/%s/%s  (id=%s)",
                tipo_operacion, fecha, dest_name, file_["id"],
            )
            return file_["id"]

        metadata  = {"name": dest_name, "parents": [fecha_id]}
        media     = MediaFileUpload(str(pdf_path), mimetype="application/pdf", resumable=False)
        file_ = (
            self._svc.files()
            .create(body=metadata, media_body=media, fields="id, name", supportsAllDrives=True)
            .execute()
        )

        logger.info(
            "Drive upload OK: %s/%s/%s  (id=%s)",
            tipo_operacion, fecha, dest_name, file_["id"],
        )
        return file_["id"]

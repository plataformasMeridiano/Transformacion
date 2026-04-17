"""
run_puente_fix_nombres.py

Corrige los boletos de Puente que quedaron con el idMovimiento como nombre
(ej: 16437291.pdf) en lugar del número de boleto real (ej: 9304.pdf).

Por cada fecha afectada:
  1. Borra los archivos locales con nombre incorrecto (8+ dígitos, patrón 16/165xxxxx)
  2. Re-descarga los PDFs (el scraper ahora usa Content-Disposition para nombrarlos)
  3. Sube los nuevos archivos a Drive
  4. Borra de Drive los archivos viejos con nombre incorrecto
"""
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

from batch_download import _resolve_env, setup_logging
from drive_uploader import DriveUploader
from scrapers.alyc_sistemaA import PuenteScraper

# ── Fechas con nombres incorrectos (idMovimiento en vez de nro boleto) ────────
FECHAS_MALAS = [
    "2026-01-15", "2026-01-16", "2026-01-19", "2026-01-20", "2026-01-21",
    "2026-01-22", "2026-01-23", "2026-01-26", "2026-01-27", "2026-01-28",
    "2026-01-29", "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05",
    "2026-02-06", "2026-02-09", "2026-02-10", "2026-02-11", "2026-02-12",
    "2026-02-13", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-02-24", "2026-02-25", "2026-02-26", "2026-02-27", "2026-03-02",
    "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09",
    "2026-03-10", "2026-03-11", "2026-03-12",
]

# Patrón de nombre incorrecto: solo dígitos, empieza con 16 o 165, longitud >= 7
_RE_NOMBRE_MALO = re.compile(r"^1[5-9]\d{5,}$")

_FOLDER_MIME = "application/vnd.google-apps.folder"
_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _nombre_malo(stem: str) -> bool:
    return bool(_RE_NOMBRE_MALO.match(stem))


def _build_drive_svc(credentials_file: str):
    creds = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=_SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_and_delete_drive_files(svc, root_folder_id: str, fecha: str, logger):
    """
    Busca en Drive archivos de Puente para la fecha dada cuyo nombre contiene
    un idMovimiento (patrón 'Boleto - Puente - 16xxxxxx.pdf') y los elimina.
    """
    # Buscar carpetas de fecha bajo cada tipo de operación
    for tipo in ("Cauciones", "Pases"):
        # Navegar root → tipo → fecha
        tipo_results = svc.files().list(
            q=(f"name = '{tipo}' and mimeType = '{_FOLDER_MIME}'"
               f" and '{root_folder_id}' in parents and trashed = false"),
            fields="files(id)", pageSize=5,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute().get("files", [])
        if not tipo_results:
            continue
        tipo_id = tipo_results[0]["id"]

        fecha_results = svc.files().list(
            q=(f"name = '{fecha}' and mimeType = '{_FOLDER_MIME}'"
               f" and '{tipo_id}' in parents and trashed = false"),
            fields="files(id)", pageSize=5,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute().get("files", [])
        if not fecha_results:
            continue
        fecha_id = fecha_results[0]["id"]

        # Listar todos los PDFs de Puente en esa carpeta
        page_token = None
        while True:
            resp = svc.files().list(
                q=(f"'{fecha_id}' in parents"
                   f" and name contains 'Puente'"
                   f" and mimeType = 'application/pdf'"
                   f" and trashed = false"),
                fields="files(id, name), nextPageToken",
                pageSize=100,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                pageToken=page_token,
            ).execute()

            for f in resp.get("files", []):
                stem = Path(f["name"]).stem  # "Boleto - Puente - 16437291"
                parts = stem.split(" - ")
                nro = parts[-1] if parts else ""
                if _nombre_malo(nro):
                    logger.info("Drive DELETE: %s/%s/%s  (id=%s)",
                                tipo, fecha, f["name"], f["id"])
                    svc.files().delete(
                        fileId=f["id"], supportsAllDrives=True
                    ).execute()

            page_token = resp.get("nextPageToken")
            if not page_token:
                break


async def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = {**config["general"], "headless": False}

    setup_logging(general["log_dir"], "puente_fix_nombres")
    logger = logging.getLogger("puente_fix_nombres")
    logger.info("=" * 60)
    logger.info("Puente fix nombres — %d fechas a corregir", len(FECHAS_MALAS))

    gd = config["google_drive"]
    root_folder_id = _resolve_env(gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)
    drive_svc = _build_drive_svc(gd["credentials_file"])

    alyc_cfg = next(a for a in config["alycs"] if a.get("activo") and a["nombre"] == "Puente")

    total_desc = total_sub = total_err = 0

    async with PuenteScraper(alyc_cfg, general) as scraper:
        logger.info("Login...")
        await scraper.login()
        logger.info("Login OK")

        for fecha in FECHAS_MALAS:
            logger.info("-" * 50)
            logger.info("Procesando %s", fecha)

            dest_dir = Path(general["download_dir"]) / "Puente" / fecha

            # ── 1. Borrar locales con nombre malo ──────────────────────────
            borrados = 0
            for pdf in dest_dir.rglob("*.pdf"):
                if _nombre_malo(pdf.stem):
                    logger.info("  Local DELETE: %s", pdf)
                    pdf.unlink()
                    borrados += 1
            logger.info("  Locales borrados: %d", borrados)

            # ── 2. Re-descargar ────────────────────────────────────────────
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                pdfs = await scraper.download_tickets(fecha, dest_dir)
                total_desc += len(pdfs)
                logger.info("  Descargados: %d", len(pdfs))
            except Exception as exc:
                logger.error("  ERROR descarga %s — %s", fecha, exc)
                total_err += 1
                continue

            # ── 3. Subir nuevos a Drive ────────────────────────────────────
            from drive_uploader import nro_from_filename
            for pdf_path in pdfs:
                tipo_operacion = pdf_path.parent.name
                nro_boleto = nro_from_filename(pdf_path.name)
                try:
                    file_id = uploader.upload_boleto(
                        pdf_path=pdf_path,
                        tipo_operacion=tipo_operacion,
                        fecha=fecha,
                        alyc_nombre="Puente",
                        nro_boleto=nro_boleto,
                    )
                    logger.info("  Drive UP: %s/%s  id=%s", tipo_operacion, pdf_path.name, file_id)
                    total_sub += 1
                except Exception as exc:
                    logger.error("  Drive FALLÓ: %s — %s", pdf_path.name, exc)
                    total_err += 1

            # ── 4. Borrar viejos en Drive ──────────────────────────────────
            try:
                _find_and_delete_drive_files(drive_svc, root_folder_id, fecha, logger)
            except Exception as exc:
                logger.error("  Drive delete falló para %s — %s", fecha, exc)

    logger.info("=" * 60)
    logger.info("RESUMEN FINAL: desc=%d  sub=%d  err=%d", total_desc, total_sub, total_err)
    logger.info("=" * 60)
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

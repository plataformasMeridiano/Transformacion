"""
run_cauciones_zapier.py — Procesa fechas con Cauciones via Zapier webhook.

Para cada fecha con boletos de Cauciones en Drive (desde 2026-01-15):
  1. Verifica si ya fue completada en Supabase (registro Conosur para esa fecha).
  2. Dispara el webhook de Zapier con la fecha.
  3. Espera hasta que Supabase muestre un registro de Conosur (máx. 30 min).
  4. Pasa a la siguiente fecha.

Uso:
    python3 run_cauciones_zapier.py
    python3 run_cauciones_zapier.py 2026-02-01   # desde una fecha específica
"""

import json
import logging
import sys
import time
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Configuración ──────────────────────────────────────────────────────────────

DESDE           = date(2026, 1, 15)
WEBHOOK_URL     = "https://hooks.zapier.com/hooks/catch/24963922/uqqfupo/"
POLL_INTERVAL_S = 60      # segundos entre consultas a Supabase
MAX_WAIT_S      = 30 * 60  # 30 minutos máximo por fecha

_FOLDER_MIME = "application/vnd.google-apps.folder"

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    log_path = Path("logs/cauciones_zapier.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(ch)

# ── Drive — listar fechas con Cauciones ────────────────────────────────────────

def get_cauciones_dates(credentials_file: str, root_folder_id: str, desde: date) -> list[str]:
    """
    Devuelve la lista de subcarpetas de fecha bajo root/Cauciones/ que sean >= desde,
    ordenadas cronológicamente.
    """
    creds = service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    # 1. Buscar carpeta "Cauciones" bajo root
    q = (
        f"name = 'Cauciones'"
        f" and mimeType = '{_FOLDER_MIME}'"
        f" and '{root_folder_id}' in parents"
        f" and trashed = false"
    )
    res = svc.files().list(
        q=q, fields="files(id, name)",
        includeItemsFromAllDrives=True, supportsAllDrives=True,
    ).execute()
    folders = res.get("files", [])
    if not folders:
        raise RuntimeError("No se encontró la carpeta 'Cauciones' en Drive")
    cauciones_id = folders[0]["id"]
    logging.info("Carpeta Cauciones en Drive: id=%s", cauciones_id)

    # 2. Listar todas las subcarpetas de fecha
    q2 = (
        f"mimeType = '{_FOLDER_MIME}'"
        f" and '{cauciones_id}' in parents"
        f" and trashed = false"
    )
    date_folders: list[str] = []
    page_token = None
    while True:
        params = dict(
            q=q2, fields="nextPageToken, files(name)",
            pageSize=1000,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
        )
        if page_token:
            params["pageToken"] = page_token
        res2 = svc.files().list(**params).execute()
        for f in res2.get("files", []):
            name = f["name"]
            try:
                d = date.fromisoformat(name)
                if d >= desde:
                    date_folders.append(name)
            except ValueError:
                pass  # carpeta con nombre que no es fecha — ignorar
        page_token = res2.get("nextPageToken")
        if not page_token:
            break

    date_folders.sort()
    logging.info("Fechas con Cauciones en Drive (>= %s): %s", desde, date_folders)
    return date_folders

# ── Supabase ───────────────────────────────────────────────────────────────────

def supabase_get(url: str, key: str, fecha: str) -> list[dict]:
    """
    Consulta Supabase: devuelve los registros de Procesamiento_Cauciones
    para la fecha indicada.
    """
    endpoint = (
        f"{url}/rest/v1/Procesamiento_Cauciones"
        f"?fecha_operacion=eq.{fecha}"
        f"&select=id,fecha_operacion,alyc,status"
    )
    req = urllib.request.Request(
        endpoint,
        headers={
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Accept":        "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logging.error("Supabase HTTP %s: %s", e.code, e.read())
        return []
    except Exception as e:
        logging.error("Supabase error: %s", e)
        return []


def is_conosur_done(records: list[dict]) -> bool:
    """Retorna True si hay al menos un registro de Conosur para la fecha."""
    for r in records:
        if "conosur" in (r.get("alyc") or "").lower():
            return True
    return False

# ── Zapier ─────────────────────────────────────────────────────────────────────

def trigger_zapier(fecha: str) -> bool:
    """Dispara el webhook de Zapier para la fecha. Retorna True si fue exitoso."""
    url = f"{WEBHOOK_URL}?fecha={fecha}"
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            logging.info("Zapier OK [%s]: status=%s  body=%s", fecha, resp.status, body[:200])
            return True
    except Exception as e:
        logging.error("Zapier error [%s]: %s", fecha, e)
        return False

# ── Loop principal ─────────────────────────────────────────────────────────────

def process_fecha(fecha: str, supabase_url: str, supabase_key: str) -> bool:
    """
    Dispara Zapier para la fecha y espera hasta que Conosur aparezca en Supabase.
    Retorna True si completó, False si agotó el tiempo.
    """
    logging.info("=" * 60)
    logging.info("Procesando fecha: %s", fecha)

    # Verificar si ya completó antes de disparar
    records = supabase_get(supabase_url, supabase_key, fecha)
    if is_conosur_done(records):
        logging.info("[%s] Ya completado en Supabase (Conosur encontrado) — saltando", fecha)
        return True

    # Disparar webhook
    if not trigger_zapier(fecha):
        logging.error("[%s] No se pudo disparar el webhook — saltando", fecha)
        return False

    # Esperar confirmación de Supabase
    elapsed = 0
    while elapsed < MAX_WAIT_S:
        logging.info("[%s] Esperando %ds...", fecha, POLL_INTERVAL_S)
        time.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S

        records = supabase_get(supabase_url, supabase_key, fecha)
        logging.info("[%s] Registros Supabase (%ds): %d", fecha, elapsed, len(records))
        for r in records:
            logging.info("  alyc=%-20s  status=%s", r.get("alyc"), r.get("status"))

        if is_conosur_done(records):
            logging.info("[%s] Completado! (Conosur encontrado en %ds)", fecha, elapsed)
            return True

    logging.error("[%s] Timeout (%ds) — Conosur nunca apareció", fecha, MAX_WAIT_S)
    return False


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    setup_logging()

    import os
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_KEY", "")
    if not supabase_url or not supabase_key:
        logging.error("Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return 1

    # Fecha de inicio (CLI arg opcional)
    desde = DESDE
    if len(sys.argv) >= 2:
        desde = date.fromisoformat(sys.argv[1])
        logging.info("Fecha de inicio por CLI: %s", desde)

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    gd = config["google_drive"]
    creds_file = gd["credentials_file"]
    root_id = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "")

    logging.info("=" * 60)
    logging.info("run_cauciones_zapier — desde %s", desde)
    logging.info("Drive root: %s", root_id)

    # 1. Obtener fechas con Cauciones en Drive
    fechas = get_cauciones_dates(creds_file, root_id, desde)
    if not fechas:
        logging.info("Sin fechas con Cauciones desde %s — nada que hacer", desde)
        return 0

    logging.info("Fechas a procesar: %d", len(fechas))

    # 2. Procesar secuencialmente
    ok = err = skip = 0
    for fecha in fechas:
        records_pre = supabase_get(supabase_url, supabase_key, fecha)
        if is_conosur_done(records_pre):
            logging.info("[%s] Ya completo — saltando", fecha)
            skip += 1
            continue

        success = process_fecha(fecha, supabase_url, supabase_key)
        if success:
            ok += 1
        else:
            err += 1

    logging.info("=" * 60)
    logging.info("RESUMEN: ok=%d  err=%d  skip=%d  total=%d", ok, err, skip, len(fechas))
    logging.info("=" * 60)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

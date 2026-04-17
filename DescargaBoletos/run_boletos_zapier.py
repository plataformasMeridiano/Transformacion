"""
run_boletos_zapier.py — Procesa fechas con Boletos (Cauciones + Pases) via Zapier webhook.

Diferencias con run_cauciones_zapier.py:
  - Obtiene fechas de AMBAS carpetas Drive: Cauciones/ y Pases/ (unión, ordenada).
  - Condición de completado: >= 2 registros de ConoSur en Supabase (uno por Pases,
    otro por Cauciones — la BD no distingue entre tipos de operación).

Para cada fecha con boletos en Drive (desde 2026-01-15):
  1. Verifica si ya fue completada en Supabase (2 registros ConoSur).
  2. Dispara el webhook de Zapier con la fecha.
  3. Espera hasta que Supabase muestre 2 registros de ConoSur (máx. 30 min).
  4. Pasa a la siguiente fecha.

Uso:
    python3 run_boletos_zapier.py
    python3 run_boletos_zapier.py 2026-02-01   # desde una fecha específica
"""

import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Configuración ──────────────────────────────────────────────────────────────

DESDE           = date(2026, 1, 15)
WEBHOOK_URL     = "https://hooks.zapier.com/hooks/catch/24963922/uqqfupo/"
POLL_INTERVAL_S = 60       # segundos entre consultas a Supabase
MAX_WAIT_S      = 30 * 60  # 30 minutos máximo por fecha
MAX_WORKERS     = 5        # fechas procesadas en paralelo

_FOLDER_MIME = "application/vnd.google-apps.folder"

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    tag = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = Path(f"logs/boletos_zapier_{tag}.log")
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
    logging.info("Log: %s", log_path)

# ── Drive — listar fechas ──────────────────────────────────────────────────────

def _list_date_subfolders(
    svc,
    parent_id: str,
    desde: date,
    folder_name: str,
) -> set[str]:
    """Devuelve el set de subcarpetas de fecha (YYYY-MM-DD) bajo parent_id/folder_name/ >= desde."""
    # 1. Buscar la carpeta tipo (Cauciones / Pases) bajo parent
    q = (
        f"name = '{folder_name}'"
        f" and mimeType = '{_FOLDER_MIME}'"
        f" and '{parent_id}' in parents"
        f" and trashed = false"
    )
    res = svc.files().list(
        q=q, fields="files(id, name)",
        includeItemsFromAllDrives=True, supportsAllDrives=True,
    ).execute()
    folders = res.get("files", [])
    if not folders:
        logging.warning("Carpeta '%s' no encontrada en Drive root", folder_name)
        return set()
    tipo_id = folders[0]["id"]
    logging.info("Carpeta %s en Drive: id=%s", folder_name, tipo_id)

    # 2. Listar subcarpetas de fecha
    q2 = (
        f"mimeType = '{_FOLDER_MIME}'"
        f" and '{tipo_id}' in parents"
        f" and trashed = false"
    )
    dates: set[str] = set()
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
                    dates.add(name)
            except ValueError:
                pass
        page_token = res2.get("nextPageToken")
        if not page_token:
            break

    logging.info("  %s: %d fechas >= %s", folder_name, len(dates), desde)
    return dates


def get_boletos_dates(credentials_file: str, root_folder_id: str, desde: date) -> list[str]:
    """
    Devuelve la lista ordenada de fechas que tienen boletos en Drive (Cauciones O Pases),
    con fecha >= desde.
    """
    creds = service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    cauciones = _list_date_subfolders(svc, root_folder_id, desde, "Cauciones")
    pases     = _list_date_subfolders(svc, root_folder_id, desde, "Pases")

    all_dates = sorted(cauciones | pases, reverse=True)  # más reciente primero
    logging.info("Total fechas únicas (Cauciones ∪ Pases) >= %s: %d", desde, len(all_dates))
    return all_dates

# ── Supabase ───────────────────────────────────────────────────────────────────

def supabase_get(url: str, key: str, fecha: str) -> list[dict]:
    """Devuelve los registros de Procesamiento_Cauciones para la fecha indicada."""
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


def is_done(records: list[dict]) -> bool:
    """Retorna True si existen registros con status 'Fin Cauciones' y 'Fin Pases'."""
    statuses = {r.get("status") for r in records}
    return "Fin Cauciones" in statuses and "Fin Pases" in statuses

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
    Dispara Zapier para la fecha y espera hasta que haya >= CONOSUR_TARGET
    registros ConoSur en Supabase. Retorna True si completó, False si timeout.
    """
    logging.info("=" * 60)
    logging.info("Procesando fecha: %s", fecha)

    logging.info("[%s] Disparando webhook", fecha)

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
        logging.info("[%s] Registros Supabase (%ds): %d total",
                     fecha, elapsed, len(records))
        for r in records:
            logging.info("  alyc=%-25s  status=%s", r.get("alyc"), r.get("status"))

        if is_done(records):
            logging.info("[%s] Completado! (status=Fin encontrado en %ds)", fecha, elapsed)
            return True

    logging.error("[%s] Timeout (%ds) — nunca apareció status=Fin", fecha, MAX_WAIT_S)
    return False


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    setup_logging()

    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_KEY", "")
    if not supabase_url or not supabase_key:
        logging.error("Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return 1

    # Fecha de inicio y fin (CLI args opcionales)
    # Uso: python3 run_boletos_zapier.py [desde] [hasta]
    # Las fechas son inclusivas; el orden de procesamiento es más reciente primero.
    desde = DESDE
    hasta = None
    if len(sys.argv) >= 2:
        desde = date.fromisoformat(sys.argv[1])
        logging.info("Fecha de inicio por CLI: %s", desde)
    if len(sys.argv) >= 3:
        hasta = date.fromisoformat(sys.argv[2])
        logging.info("Fecha de fin por CLI: %s", hasta)

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    gd = config["google_drive"]
    creds_file = gd["credentials_file"]
    root_id    = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "")
    if not root_id:
        # Intentar resolver desde config si tiene el valor directo
        raw = gd.get("root_folder_id", "")
        root_id = raw if not raw.startswith("${") else ""
    if not root_id:
        logging.error("Falta GDRIVE_ROOT_FOLDER_ID en .env")
        return 1

    logging.info("=" * 60)
    logging.info("run_boletos_zapier — desde %s", desde)
    logging.info("Drive root: %s", root_id)

    # 1. Obtener fechas con boletos en Drive (Cauciones + Pases)
    fechas = get_boletos_dates(creds_file, root_id, desde)
    if hasta:
        fechas = [f for f in fechas if date.fromisoformat(f) <= hasta]
        logging.info("Fechas filtradas hasta %s: %d", hasta, len(fechas))
    if not fechas:
        logging.info("Sin fechas con boletos desde %s — nada que hacer", desde)
        return 0

    logging.info("Fechas a procesar: %d  (paralelo: %d workers)", len(fechas), MAX_WORKERS)

    pendientes = fechas
    logging.info("Fechas a procesar: %d", len(pendientes))

    # 3. Procesar en paralelo (hasta MAX_WORKERS fechas simultáneas)
    ok = err = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_fecha, fecha, supabase_url, supabase_key): fecha
            for fecha in pendientes
        }
        for fut in as_completed(futures):
            fecha = futures[fut]
            try:
                success = fut.result()
            except Exception as exc:
                logging.error("[%s] Excepción inesperada: %s", fecha, exc)
                success = False
            if success:
                ok += 1
            else:
                err += 1

    logging.info("=" * 60)
    logging.info("RESUMEN: ok=%d  err=%d  total=%d", ok, err, len(fechas))
    logging.info("=" * 60)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

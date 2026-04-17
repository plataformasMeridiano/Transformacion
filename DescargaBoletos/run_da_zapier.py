"""
run_da_zapier.py — Dispara Zapier solo para DAValores en las fechas con boletos descargados.
"""
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

WEBHOOK_URL   = "https://hooks.zapier.com/hooks/catch/24963922/uqqfupo/"
ALYC          = "DAValores"
POLL_INTERVAL = 30
MAX_WAIT      = 10 * 60  # 10 min por fecha

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
supabase_key = os.environ.get("SUPABASE_KEY", "")


def get_fechas() -> list[str]:
    base = Path(__file__).parent / "downloads" / "DAValores"
    fechas = sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and any(d.rglob("*.pdf"))
    )
    return fechas


def trigger(fecha: str) -> bool:
    url = f"{WEBHOOK_URL}?fecha={fecha}&alyc={ALYC}"
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            logging.info("Zapier OK [%s]: %s", fecha, body[:80])
            return True
    except Exception as e:
        logging.error("Zapier ERROR [%s]: %s", fecha, e)
        return False


def wait_done(fecha: str) -> bool:
    elapsed = 0
    while elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        try:
            endpoint = (
                f"{supabase_url}/rest/v1/Procesamiento_Cauciones"
                f"?fecha_operacion=eq.{fecha}"
                f"&alyc=like.*{ALYC}*"
                f"&select=alyc,status"
            )
            req = urllib.request.Request(
                endpoint,
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                records = json.loads(r.read())
        except Exception as e:
            logging.warning("[%s] Supabase error: %s", fecha, e)
            continue

        logging.info("[%s] %ds — registros DA: %d", fecha, elapsed, len(records))
        for rec in records:
            logging.info("  alyc=%-30s  status=%s", rec.get("alyc"), rec.get("status"))

        statuses = {rec.get("status") for rec in records}
        if any("Fin" in (s or "") for s in statuses):
            logging.info("[%s] Completado!", fecha)
            return True

    logging.error("[%s] Timeout (%ds)", fecha, MAX_WAIT)
    return False


def main():
    fechas = get_fechas()
    logging.info("Fechas con boletos DA Valores: %d", len(fechas))
    for f in fechas:
        logging.info("  %s", f)

    ok = err = 0
    for fecha in fechas:
        logging.info("=" * 50)
        if not trigger(fecha):
            err += 1
            continue
        if wait_done(fecha):
            ok += 1
        else:
            err += 1

    logging.info("=" * 50)
    logging.info("RESUMEN: ok=%d  err=%d  total=%d", ok, err, len(fechas))
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""
cleanup_conosur_pases_drive.py — Mueve a la papelera de Drive los boletos de
Pases ConoSur que no corresponden a pares legítimos (Venta+Compra mismo simboloLocal).

Los archivos se mueven a la papelera (trashed=True) — son recuperables.

FASE 1 (ejecutar ahora):
    26 boletos identificados comparando el batch original (old logic)
    con el fix run (new _match_pases logic) para ene-26 a feb-18.

FASE 2 (ejecutar después de run_conosur_fix_retry_mn.py):
    Boletos incorrectos para feb-19 a mar-12 (MN).
    Están identificados en el comentario al final del script.

Uso:
    python3 cleanup_conosur_pases_drive.py [--fase2]
"""
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

_SCOPES = ["https://www.googleapis.com/auth/drive"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cleanup")

# ── FASE 1: boletos en batch original que NO están en el fix run ─────────────
# Identificados con: comm -23 original_pases.txt correct_pases.txt
# fmt: off
FASE1 = [
    # (fecha,        nro_boleto,    drive_file_id)
    ("2026-01-26", "2026026822", "1w6VpCjWqNlAikg-nybe9IXTUwyn2RHFr"),
    ("2026-01-30", "2026000823", "10f2fbvuhBt3rSg2tPz9mzxPw4okU8q88"),
    ("2026-01-30", "2026000827", "1XOamzt4NcYssfCs3GBU9PqFMuFuMfGGr"),
    ("2026-02-02", "2026001474", "1W2F_7fttDpVMsfSIMP0M2uDS7OycPoGE"),
    ("2026-02-02", "2026002414", "1rDJ_JD_dLFq7MV45YUeYwhCfsNZ2n5Gc"),
    ("2026-02-03", "2026001270", "1pbVqhmHRkehM0khXTTTUYOIcoFbWk5l4"),
    ("2026-02-03", "2026001498", "1EUilGY_hadehft9t2yhTal9GXgfbO6_H"),
    ("2026-02-03", "2026005218", "1Q8gAtnbnyDx7iFfCQTz5F5CGulPGjYdP"),
    ("2026-02-04", "2026000977", "1I6kHVYvocwH5atRnY-KaD5VgmrgdNsKp"),
    ("2026-02-04", "2026001372", "1OS-1IyUVcbM7hOe8sjGeJJI72fY8GKN0"),
    ("2026-02-04", "2026001375", "1h7i2X7I61r9hkLCUfiBotLJiJrzLFVuM"),
    ("2026-02-04", "2026001622", "108_af3c3LpuxgPjpPmA5xFsCJ0F9fYI8"),
    ("2026-02-04", "2026001628", "1z943eWOh6iDip9OYj4ED-RF9AMBoEeSx"),
    ("2026-02-04", "2026005332", "135lLAUf0oQxfXW8CWlvWnCm4MjA_QH68"),
    ("2026-02-04", "2026005336", "1XiESZpW5s2TGAduFWjG5kaHYwiPLsczV"),
    ("2026-02-11", "2026001163", "1HJ2eEAyAu15C24R7fm0noinZ_dVQ-2sl"),
    ("2026-02-12", "2026001188", "1-QfxiWEJkThFXs1ag8TxLK9e-jMiI7wS"),
    ("2026-02-12", "2026003228", "1zLC5rUeswRCXMMmOXo1HdgE7-G4CdrYr"),
    ("2026-02-13", "2026001776", "1Nz0E3U8HMz5CZq1hM5-inDuacjGEmTcn"),
    ("2026-02-13", "2026002074", "1uCZ_8sMPdBxNxoIOSiYy5BAprf9yKfv3"),
    ("2026-02-13", "2026006402", "157jhdJuIYSUl5jPZNi1v-UpJFLwbtpMk"),
    ("2026-02-13", "2026054369", "1xVff-byOEXUZfbueJLXMNhCWGmuXPUJG"),
    ("2026-02-13", "2026054372", "1QxqFbT-wzeQAzh8dT9WKsC7NPYbgo7wt"),
    ("2026-02-18", "2026001838", "12dz8kns8XLnlztbiG0TAnpAHJfWipBTv"),
    ("2026-02-18", "2026002137", "1eeHC5ckPu0maYXID5XSMtitpP7eIBmnf"),
    ("2026-02-18", "2026006553", "1VMFJHIS4bHG2p8PIwy02zWgAyoU2RsSt"),
]

# ── FASE 2: boletos de mar batch para feb-19 a mar-12 ────────────────────────
# Ejecutar SOLO después de run_conosur_fix_retry_mn.py
# Identificar con:
#   grep "Drive upload OK: Pases/\|Drive update OK: Pases/" logs/batch_2026-03-01_a_2026-03-12.log \
#     | grep ConoSur | sed 's/.*Pases\/([^/]*)\/.*/\1 \2/'
# vs retry log (logs/batch_conosur_mn_retry_2026-02-19_a_2026-03-12.log)
# El boleto sospechoso 2026000874 (2026-03-02) y similares de baja numeración.
FASE2 = [
    # 1 boleto incorrecto identificado comparando batch_2026-03-01 vs retry log.
    # Feb-19 a Feb-27: JWT expiró en ambos batches anteriores → sin uploads incorrectos.
    ("2026-03-02", "2026000874", "14kZNsnum0eOcD_cR4bS-9NUwFFAIWzdN"),
]
# fmt: on


def build_drive_service(credentials_file: str):
    creds = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=_SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def trash_files(svc, entries: list[tuple[str, str, str]], dry_run: bool = False) -> tuple[int, int]:
    """Mueve a la papelera cada archivo en la lista. Retorna (eliminados, errores)."""
    trashed = errors = 0
    for fecha, nro, fid in entries:
        label = f"Pases/{fecha}/Boleto - ConoSur - {nro}.pdf"
        if dry_run:
            logger.info("DRY RUN — would trash: %s  (id=%s)", label, fid)
            trashed += 1
            continue
        try:
            svc.files().update(
                fileId=fid,
                body={"trashed": True},
                supportsAllDrives=True,
            ).execute()
            logger.info("TRASHED: %s  (id=%s)", label, fid)
            trashed += 1
        except Exception as exc:
            logger.error("ERROR trashing %s  (id=%s): %s", label, fid, exc)
            errors += 1
    return trashed, errors


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    run_fase2 = "--fase2" in sys.argv
    dry_run   = "--dry-run" in sys.argv

    svc = build_drive_service(config["google_drive"]["credentials_file"])

    # ── Fase 1 ────────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("FASE 1 — %d boletos incorrectos (ene-26 a feb-18)", len(FASE1))
    t1, e1 = trash_files(svc, FASE1, dry_run=dry_run)
    logger.info("Fase 1: %d movidos a papelera, %d errores", t1, e1)

    # ── Fase 2 (opcional) ─────────────────────────────────────────────────────
    t2 = e2 = 0
    if run_fase2:
        if not FASE2:
            logger.warning("FASE 2 vacía — completar FASE2 en el script con IDs del retry log")
        else:
            logger.info("=" * 60)
            logger.info("FASE 2 — %d boletos incorrectos (feb-19 a mar-12)", len(FASE2))
            t2, e2 = trash_files(svc, FASE2, dry_run=dry_run)
            logger.info("Fase 2: %d movidos a papelera, %d errores", t2, e2)

    logger.info("=" * 60)
    logger.info("TOTAL: %d movidos a papelera, %d errores", t1 + t2, e1 + e2)
    return 0 if (e1 + e2) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

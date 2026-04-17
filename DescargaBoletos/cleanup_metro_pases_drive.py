"""
cleanup_metro_pases_drive.py — Mueve a la papelera los boletos MetroCorp incorrectamente
clasificados como Pases en Drive.

Root cause: "TRANSF RECEPTORA INTERNA TERCEROS C VALORES" son transferencias de garantía
colateral de cauciones (no pases bursátiles), sin numeroBoleto propio. La clasificación
_classify() los mandaba a Pases porque no contienen "CAUC" ni "COLOCACION".

Fix aplicado: config.json MetroCorp tipo_operacion → ["Cauciones"] solamente.

Archivos a limpiar: todos los MetroCorp Pases subidos entre ene-20 y mar-05.
Se mueven a la papelera (recuperables). Los originales de batch_2026-01-15 tenían
nombres 001.pdf/002.pdf; el redownload agregó nombres propios (255082.pdf, etc.)
por lo que en algunos días hay 4 archivos.

Uso:
    python3 cleanup_metro_pases_drive.py [--dry-run]
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
logger = logging.getLogger("cleanup_metro")

# fmt: off
# Todos los MetroCorp Pases incorrectos en Drive
# (fecha, nro_o_nombre, drive_file_id)
PASES_INCORRECTOS = [
    # ── 2026-01-20 (batch original + redownload) ──────────────────────────────
    ("2026-01-20", "001",    "1ls9g0L9lKTtJNYwtB3_ke1auyviKUCh7"),
    ("2026-01-20", "002",    "1cMO9wT2rTyd-W0V_fykS4c5j_OsegJvm"),
    ("2026-01-20", "255082", "1YFUXSi6mKCisL535BO6Z5XNkW-T_vUld"),
    ("2026-01-20", "255081", "1ua7zGFJAsJO5tAz7Mro4ZqohZoGrEWCw"),
    # ── 2026-01-27 (batch original + redownload) ──────────────────────────────
    ("2026-01-27", "001",    "1FcSYKjJ-va-OmYqfAt9JeEzcd1-EgIzU"),
    ("2026-01-27", "002",    "1dRQVvxpJ-dqOa2wlaLO-q09q1ao9G7pg"),
    ("2026-01-27", "255368", "12UiaeOuk-AH55-_TZze-YvN14WAPz0yA"),
    ("2026-01-27", "255364", "1bKRXm8wc4oKJY2n3pMI6tCOozkW3u6d6"),
    # ── 2026-01-28 ────────────────────────────────────────────────────────────
    ("2026-01-28", "001",    "1BpF5m0weI9_vmd974mXyxNwfeHFhbDEa"),
    ("2026-01-28", "002",    "12FbzbNyeH8FPDdPBq3-l_thaSVhw2HSx"),
    # ── 2026-01-29 ────────────────────────────────────────────────────────────
    ("2026-01-29", "001",    "1NVtWMJKe8j_eLGDC1DYWFa_WEjLiW6BL"),
    ("2026-01-29", "002",    "1W6-ZIPmhkTGtzEsEZIKmKz2h-hjsHRmv"),
    # ── 2026-01-30 ────────────────────────────────────────────────────────────
    ("2026-01-30", "001",    "1h0vUcVRcBw8I0nWNgJQV9YTo9ro5fmlw"),
    ("2026-01-30", "002",    "1nzO8IyFWhb2mSW7lx-6dfsmONtMRAqtu"),
    # ── 2026-02-02 ────────────────────────────────────────────────────────────
    ("2026-02-02", "001",    "1k6h1TN2V_cMkHmdzo5iimE58ECz_MrKo"),
    ("2026-02-02", "002",    "1U8Qo8pvJJS4ZTvXqDKMBq7bwq0mf_uve"),
    # ── 2026-02-04 ────────────────────────────────────────────────────────────
    ("2026-02-04", "001",    "1iEhACJajJw8_r1Kj1_g6s2uq-CK_SJr_"),
    # ── 2026-02-05 ────────────────────────────────────────────────────────────
    ("2026-02-05", "001",    "1-3e2sAm97ojCEN_7Wa-niuBNvlADXyn3"),
    ("2026-02-05", "002",    "1vDvmuy2oYiZ1uYDX9yzhAXsQaxG3JMSR"),
    # ── 2026-02-06 ────────────────────────────────────────────────────────────
    ("2026-02-06", "001",    "1lJzPZ2h-XucUwIqRs0aTMimfqcfFP-u3"),
    ("2026-02-06", "002",    "1LAo_8c9_NhEKTeJ3ErkoeFRrBEcMa9tv"),
    # ── 2026-02-09 (4 items — día de mayor actividad) ─────────────────────────
    ("2026-02-09", "001",    "1U5Dx7VOMmWXjqAhOMjvdbjo7BLWp5fxn"),
    ("2026-02-09", "002",    "1iskKesxAv_pEXrKJTob46Ab527xyhgIo"),
    ("2026-02-09", "003",    "16F680SGeEiMSAmjn--4mNAW9PUEvrQm7"),
    ("2026-02-09", "004",    "17XvpzsZsERHPQhMGImCxw-ErEvEsn9c1"),
    # ── 2026-02-10 ────────────────────────────────────────────────────────────
    ("2026-02-10", "001",    "1V2nbEBJXWbIjzsttgXG5yXIiRYZ2JHBz"),
    ("2026-02-10", "002",    "1B41aMkV2JvWERzcaVEI_04b6XFOG7JFu"),
    # ── 2026-02-18 (día activo, 4 pases incluyendo numBoleto 4932) ───────────
    ("2026-02-18", "4932",   "1tjb-g1eYEbbjNap51ntVZ-PVNCSFK1jT"),
    ("2026-02-18", "002",    "11bA5RH_FRS4gQQLNk-ZVJl2_P2mL1d2k"),
    ("2026-02-18", "004",    "1VQy6pfydj9jCsnBcP0zXJW1Z8wqpYcjm"),
    # ── 2026-02-25 ────────────────────────────────────────────────────────────
    ("2026-02-25", "001",    "1BFJuTLMhXix3qqsU88qY2w-Er2KuDR6I"),
    ("2026-02-25", "002",    "1dhFegNRufjdkCXf5QxKxkcLx2uRg4zfH"),
    # ── 2026-03-04 (batch mar-01 a mar-12) ────────────────────────────────────
    ("2026-03-04", "256633", "1LC0-1Fu6ThtU2wq_11B5boUkWyixpD40"),
    # ── 2026-03-05 ────────────────────────────────────────────────────────────
    ("2026-03-05", "257172", "10v7mtzHK-wT36GHuy9IFCODvagwjNwhg"),
]
# fmt: on


def build_drive_service(credentials_file: str):
    creds = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=_SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def trash_files(svc, entries, dry_run: bool = False) -> tuple[int, int]:
    trashed = errors = 0
    for fecha, nro, fid in entries:
        label = f"Pases/{fecha}/Boleto - MetroCorp - {nro}.pdf"
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

    dry_run = "--dry-run" in sys.argv
    svc = build_drive_service(config["google_drive"]["credentials_file"])

    logger.info("=" * 60)
    logger.info("Limpiando %d MetroCorp Pases incorrectos de Drive", len(PASES_INCORRECTOS))
    if dry_run:
        logger.info("(DRY RUN — no se modifica nada)")

    trashed, errors = trash_files(svc, PASES_INCORRECTOS, dry_run=dry_run)

    logger.info("=" * 60)
    logger.info("TOTAL: %d movidos a papelera, %d errores", trashed, errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

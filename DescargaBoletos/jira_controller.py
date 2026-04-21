"""
jira_controller.py — Control cruzado entre boletos descargados y issues en Jira PAS.

Para cada fecha verifica que cada boleto que subimos a Drive tenga su issue
correspondiente en el proyecto Jira PAS, consultando vía API.

Uso:
    python3 jira_controller.py 2026-04-17
    python3 jira_controller.py 2026-04-09 2026-04-17
"""
import base64
import json
import logging
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Configuración Jira ────────────────────────────────────────────────────────

_JIRA_EMAIL    = os.environ["JIRA_EMAIL"]
_JIRA_TOKEN    = os.environ["JIRA_API_TOKEN"]
_JIRA_CLOUD_ID = os.environ["JIRA_CLOUD_ID"]
_JIRA_PROJECT  = os.environ.get("JIRA_PROJECT", "PAS")
_JIRA_BASE     = f"https://api.atlassian.com/ex/jira/{_JIRA_CLOUD_ID}/rest/api/3"

# Custom fields
CF_ALYC  = "customfield_11360"  # ALyC (ej: "ConoSur", "Puente")
CF_FECHA = "customfield_10455"  # Fecha de Operación (YYYY-MM-DD)
CF_NRO   = "customfield_10807"  # Número de boleto (int)
CF_TIPO  = "customfield_10840"  # Tipo de Boleto (Caución / Pase / Venta FCE-eCheq)

# Mapeo: carpeta local → nombre ALyC en Jira (cf_alyc)
FOLDER_TO_JIRA: dict[str, str] = {
    "Allaria":    "Allaria",
    "ADCAP":      "ADCAP",
    "Criteria":   "Criteria",
    "BACS":       "BACS",
    "DAValores":  "DA Valores",
    "WIN":        "Win",
    "ConoSur":    "ConoSur",
    "MaxCapital": "Max Capital",
    "MetroCorp":  "Metrocorp",
    "Dhalmore":   "Dhalmore",
    "Puente":     "Puente",
}
JIRA_TO_FOLDER = {v: k for k, v in FOLDER_TO_JIRA.items()}

# Mapeo: tipo Jira → carpeta local
TIPO_JIRA_TO_LOCAL: dict[str, str] = {
    "Caución":        "Cauciones",
    "Pase":           "Pases",
    "Venta FCE-eCheq": "Venta FCE-eCheq",
}

ACCOUNT_DIRS = {"MeridianoNorte", "Pamat", "Clinicaltech"}
DOWNLOADS_DIR = Path(__file__).parent / "downloads"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jira_ctrl")


# ── Cliente Jira ─────────────────────────────────────────────────────────────

def _jira_headers() -> dict:
    auth = base64.b64encode(f"{_JIRA_EMAIL}:{_JIRA_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def jira_search(jql: str, fields: list[str]) -> list[dict]:
    """
    Ejecuta un JQL y retorna TODOS los issues (paginación por cursor).
    Usa /rest/api/3/search/jql (nuevo endpoint Jira Cloud).
    """
    url = f"{_JIRA_BASE}/search/jql"
    all_issues: list[dict] = []
    next_page_token: str | None = None

    while True:
        body: dict = {"jql": jql, "maxResults": 100, "fields": fields}
        if next_page_token:
            body["nextPageToken"] = next_page_token

        payload = json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload, headers=_jira_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except urllib.request.HTTPError as e:
            body_err = e.read().decode()
            raise RuntimeError(f"Jira API error {e.code}: {body_err[:300]}") from e

        issues = data.get("issues", [])
        all_issues.extend(issues)

        if data.get("isLast", True) or not issues:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return all_issues


def get_jira_issues_for_fecha(fecha: str) -> list[dict]:
    jql = f'project={_JIRA_PROJECT} AND cf[10455] = "{fecha}"'
    fields = [CF_ALYC, CF_FECHA, CF_NRO, CF_TIPO, "summary"]
    return jira_search(jql, fields)


# ── Boletos locales ───────────────────────────────────────────────────────────

def _stem_to_nro(stem: str) -> str:
    """Extrae el número de boleto del nombre de archivo."""
    s = stem
    s = re.sub(r"^BOL_",        "", s, flags=re.IGNORECASE)
    s = re.sub(r"^BOLETO_NRO_", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^BOLETO_",     "", s, flags=re.IGNORECASE)
    m = re.search(r"-\s*(\d+)\s*$", s)
    if m:
        s = m.group(1)
    return str(int(s)) if re.fullmatch(r"\d+", s) else s


def collect_local_boletos(fecha: str) -> list[dict]:
    """
    Recorre downloads/{ALYC}/{fecha}/ y retorna los boletos únicos.
    Deduplicación por (folder, tipo, nro) — MeridianoNorte y Pamat comparten boleto.
    """
    boletos: list[dict] = []
    seen: set[tuple] = set()

    for alyc_dir in sorted(DOWNLOADS_DIR.iterdir()):
        if not alyc_dir.is_dir() or alyc_dir.name not in FOLDER_TO_JIRA:
            continue
        date_dir = alyc_dir / fecha
        if not date_dir.exists():
            continue
        folder = alyc_dir.name

        def ingest(tipo_dir: Path, tipo: str):
            for pdf in sorted(tipo_dir.iterdir()):
                if pdf.suffix.lower() != ".pdf":
                    continue
                nro = _stem_to_nro(pdf.stem)
                # Puente: ignorar archivos con nombre idMovimiento (≥7 dígitos)
                # Los boletos reales de Puente tienen ≤6 dígitos; los idMovimiento son 16xxxxxx
                if folder == "Puente" and re.fullmatch(r"\d{7,}", nro):
                    continue
                key = (folder, tipo, nro)
                if key in seen:
                    continue
                seen.add(key)
                boletos.append({"folder": folder, "tipo": tipo, "nro": nro})

        for item in sorted(date_dir.iterdir()):
            if not item.is_dir():
                continue
            if item.name in ("Cauciones", "Pases", "Venta FCE-eCheq"):
                ingest(item, item.name)
            elif item.name in ACCOUNT_DIRS:
                for tipo_dir in sorted(item.iterdir()):
                    if tipo_dir.is_dir() and tipo_dir.name in ("Cauciones", "Pases", "Venta FCE-eCheq"):
                        ingest(tipo_dir, tipo_dir.name)

    return boletos


# ── Verificación ──────────────────────────────────────────────────────────────

def verify_fecha(fecha: str) -> dict:
    """
    Compara boletos locales vs issues Jira para una fecha.

    Lógica de comparación:
    - Clave de match: (folder_local, tipo_local, nro_boleto)
    - Para Cauciones: Jira crea 2 issues por boleto (concertacion + liquidacion).
      Se deduplicamos por nro en Jira antes de comparar.
    - Reporta faltantes (local sin Jira) y huérfanos (Jira sin local).
    """
    logger.info("Verificando %s...", fecha)

    local = collect_local_boletos(fecha)
    jira_raw = get_jira_issues_for_fecha(fecha)

    logger.info("  Local: %d boletos  |  Jira: %d issues", len(local), len(jira_raw))

    # Índice Jira: (folder, tipo_local, nro) → lista de issue keys
    # Deduplicamos por nro (concertacion + liquidacion comparten número)
    jira_index: dict[tuple, set[str]] = defaultdict(set)
    for iss in jira_raw:
        f = iss["fields"]
        alyc_jira = f.get(CF_ALYC) or ""
        folder    = JIRA_TO_FOLDER.get(alyc_jira, alyc_jira)
        tipo_val  = (f.get(CF_TIPO) or {}).get("value", "") if isinstance(f.get(CF_TIPO), dict) else ""
        tipo      = TIPO_JIRA_TO_LOCAL.get(tipo_val, tipo_val)
        nro_raw   = f.get(CF_NRO)
        nro       = str(int(nro_raw)) if nro_raw is not None else ""
        jira_index[(folder, tipo, nro)].add(iss["key"])

    # Índice local
    local_keys = {(b["folder"], b["tipo"], b["nro"]) for b in local}

    faltantes_en_jira = local_keys - set(jira_index.keys())
    solo_en_jira      = set(jira_index.keys()) - local_keys

    return {
        "fecha":            fecha,
        "local_count":      len(local),
        "jira_issue_count": len(jira_raw),
        "jira_boleto_count": len(jira_index),
        "faltantes":        sorted(faltantes_en_jira),
        "solo_jira":        {k: sorted(v) for k, v in jira_index.items() if k in solo_en_jira},
        "local_boletos":    local,
        "jira_boletos":     dict(jira_index),
    }


def print_result(r: dict) -> None:
    fecha = r["fecha"]
    print(f"\n{'='*65}")
    print(f"  {fecha}  —  local: {r['local_count']}  |  Jira issues: {r['jira_issue_count']}  (boletos únicos: {r['jira_boleto_count']})")
    print(f"{'='*65}")

    if r["faltantes"]:
        print(f"  !! FALTANTES EN JIRA ({len(r['faltantes'])}):")
        for folder, tipo, nro in r["faltantes"]:
            print(f"     {folder:<12}  {tipo:<18}  nro={nro}")
    else:
        print("  ✓ Todos los boletos locales tienen issue en Jira")

    if r["solo_jira"]:
        print(f"\n  ℹ  SOLO EN JIRA ({len(r['solo_jira'])}) — sin archivo local:")
        for (folder, tipo, nro), keys in sorted(r["solo_jira"].items()):
            print(f"     {folder:<12}  {tipo:<18}  nro={nro:<12}  keys={','.join(keys)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def business_days(start: date, end: date) -> list[str]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: python3 jira_controller.py FECHA [FECHA_FIN]")
        print("  FECHA: YYYY-MM-DD")
        return 1

    inicio = date.fromisoformat(sys.argv[1])
    fin    = date.fromisoformat(sys.argv[2]) if len(sys.argv) >= 3 else inicio
    fechas = business_days(inicio, fin)

    if not fechas:
        print("Sin días hábiles en el rango.")
        return 0

    logger.info("Verificando %d fecha(s): %s … %s", len(fechas), fechas[0], fechas[-1])

    resultados = [verify_fecha(f) for f in fechas]

    for r in resultados:
        print_result(r)

    # Resumen final
    total_faltantes = sum(len(r["faltantes"]) for r in resultados)
    fechas_ok       = sum(1 for r in resultados if not r["faltantes"])

    print(f"\n{'='*65}")
    print("RESUMEN FINAL")
    print(f"{'='*65}")
    print(f"  {'Fecha':<12}  {'Local':>5}  {'Jira':>5}  {'Únicos':>6}  {'Faltantes':>10}  {'SoloJira':>8}")
    for r in resultados:
        status = "OK" if not r["faltantes"] else f"!! {len(r['faltantes'])} faltantes"
        print(f"  {r['fecha']:<12}  {r['local_count']:>5}  {r['jira_issue_count']:>5}  {r['jira_boleto_count']:>6}  {status}")

    print(f"\n  Fechas OK: {fechas_ok}/{len(resultados)}  |  Boletos faltantes total: {total_faltantes}")
    return 0 if total_faltantes == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

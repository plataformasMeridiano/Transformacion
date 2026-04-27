"""
slack_notifier.py — Publica resúmenes de procesamiento en Slack.

Uso directo:
    python3 slack_notifier.py 2026-04-17
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# ALYCs en orden de visualización
_ALYC_ORDER = [
    "Allaria", "ADCAP", "BACS", "ConoSur", "Criteria",
    "DAValores", "Dhalmore", "MaxCapital", "MetroCorp", "Puente", "WIN",
]


def _post(payload: dict) -> bool:
    if not _WEBHOOK_URL:
        return False
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _WEBHOOK_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"[slack] Error: {e}", file=sys.stderr)
        return False


def send_resumen_fecha(fecha: str, resultado: dict) -> bool:
    """
    Publica en Slack la tabla de procesamiento para una fecha.

    resultado es el dict que retorna jira_controller.verify_fecha():
        local_boletos: list[dict]   — con keys folder, tipo, nro
        jira_boletos:  dict         — (folder, tipo, nro) → set[issue_key]
        faltantes:     list[tuple]
        solo_jira:     dict
    """
    local_boletos = resultado.get("local_boletos", [])
    jira_boletos  = resultado.get("jira_boletos", {})

    # Contar por (folder, tipo)
    from collections import defaultdict
    local_counts: dict[tuple, int] = defaultdict(int)
    for b in local_boletos:
        local_counts[(b["folder"], b["tipo"])] += 1

    jira_counts: dict[tuple, int] = defaultdict(int)
    for (folder, tipo, nro) in jira_boletos:
        jira_counts[(folder, tipo)] += 1

    # ALYCs con actividad
    alycs_activos = sorted(
        {folder for folder, _ in list(local_counts.keys()) + list(jira_counts.keys())},
        key=lambda x: _ALYC_ORDER.index(x) if x in _ALYC_ORDER else 99,
    )

    if not alycs_activos:
        return _post({"text": f"📭 *{fecha}* — sin boletos procesados"})

    # Construir tabla
    col_alyc = 12
    col_num  = 5

    header  = f"{'ALYC':<{col_alyc}}  {'── Cauciones ──':^{col_num*2+3}}  {'──── Pases ────':^{col_num*2+3}}"
    subhead = f"{'':^{col_alyc}}  {'PDF':>{col_num}} {'Jira':>{col_num}}    {'PDF':>{col_num}} {'Jira':>{col_num}}"
    sep     = "─" * len(header)

    rows = []
    total_cau_pdf = total_cau_jira = total_pas_pdf = total_pas_jira = 0
    total_faltantes = 0

    for folder in alycs_activos:
        cau_pdf  = local_counts.get((folder, "Cauciones"), 0)
        cau_jira = jira_counts.get((folder, "Cauciones"), 0)
        pas_pdf  = local_counts.get((folder, "Pases"), 0)
        pas_jira = jira_counts.get((folder, "Pases"), 0)

        faltantes_folder = sum(
            1 for (f, t, _) in resultado.get("faltantes", [])
            if f == folder
        )
        total_faltantes += faltantes_folder

        warn = " ⚠" if faltantes_folder else ""

        def fmt(pdf, jira):
            if pdf == 0 and jira == 0:
                return f"{'—':>{col_num}} {'—':>{col_num}}"
            return f"{pdf:>{col_num}} {jira:>{col_num}}"

        rows.append(
            f"{folder:<{col_alyc}}  {fmt(cau_pdf, cau_jira)}    {fmt(pas_pdf, pas_jira)}{warn}"
        )

        total_cau_pdf  += cau_pdf
        total_cau_jira += cau_jira
        total_pas_pdf  += pas_pdf
        total_pas_jira += pas_jira

    total_row = (
        f"{'TOTAL':<{col_alyc}}  "
        f"{total_cau_pdf:>{col_num}} {total_cau_jira:>{col_num}}    "
        f"{total_pas_pdf:>{col_num}} {total_pas_jira:>{col_num}}"
    )

    tabla = "\n".join([header, subhead, sep] + rows + [sep, total_row])

    estado = "✅" if total_faltantes == 0 else f"⚠️  {total_faltantes} faltante(s) en Jira"

    texto = (
        f"*Procesamiento {fecha}* — {estado}\n"
        f"```\n{tabla}\n```"
    )

    return _post({"text": texto})


def send_alarm(texto: str) -> bool:
    """Publica una alerta crítica en Slack (emoji 🚨)."""
    return _post({"text": f"🚨 *ALERTA — Boletos*\n{texto}"})


def send_info(texto: str) -> bool:
    """Publica un mensaje informativo en Slack."""
    return _post({"text": texto})


if __name__ == "__main__":
    # Prueba standalone: envía tabla para una fecha usando jira_controller
    if len(sys.argv) < 2:
        print("Uso: python3 slack_notifier.py FECHA")
        sys.exit(1)

    fecha = sys.argv[1]
    from jira_controller import verify_fecha
    r = verify_fecha(fecha)
    ok = send_resumen_fecha(fecha, r)
    print(f"Slack: {'OK' if ok else 'ERROR'}")

"""
daily_orchestrator.py — Orquestador del procesamiento diario de boletos.

Reemplaza run_daily.sh. Fases:
  1. Descarga    — batch_download.py --delta (Cauciones + Pases + FCE)
  2. Cocos       — upload_cocos_drive.py --desde FECHA
  3. Cobros FCE  — run_allaria_cobros_fce.py (cobros de FCE en cuenta corriente Allaria)
  4. Zapier      — run_boletos_zapier.py para los últimos días hábiles
  5. Jira        — verifica que cada boleto local tenga issue; alerta en Slack si no
  6. Resumen     — tabla por fecha en Slack

Uso (cron):
    0 12 * * 1-6 cd /ruta && xvfb-run --auto-servernum python3 daily_orchestrator.py >> logs/cron.log 2>&1
"""

import json
import logging
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from jira_controller import verify_fecha, verify_cobros_fecha
from jira_controller import business_days as _bdays
from slack_notifier import send_resumen_fecha, send_alarm, send_info

SCRIPT_DIR = Path(__file__).parent
LOG_DIR    = SCRIPT_DIR / "logs"

# Cuántos días hábiles recientes verificar en Jira (cubre fines de semana largos)
VENTANA_DIAS = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _setup_logging(tag: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(LOG_DIR / f"orchestrator_{tag}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, "%H:%M:%S"))
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, "%H:%M:%S"))
    root.addHandler(ch)


def _recent_business_days(n: int) -> list[str]:
    """Últimos n días hábiles hasta ayer (inclusive)."""
    result, d = [], date.today() - timedelta(days=1)
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d.isoformat())
        d -= timedelta(days=1)
    return list(reversed(result))


def _run(cmd: list[str], phase: str) -> int:
    logger = logging.getLogger("orchestrator")
    logger.info("─" * 55)
    logger.info("[%s] Ejecutando: %s", phase, " ".join(cmd))
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    logger.info("[%s] exit=%d", phase, result.returncode)
    return result.returncode


# ── Fases ─────────────────────────────────────────────────────────────────────

def phase_download() -> bool:
    """Descarga boletos en modo delta (todos los ALYCs, todas las operaciones)."""
    exit_code = _run(["python3", "batch_download.py", "--delta"], "1/4 download")
    if exit_code != 0:
        send_alarm(
            f"*batch_download falló* (exit={exit_code})\n"
            "Los boletos del día pueden estar incompletos. Revisar log."
        )
        return False
    return True


def phase_cocos(desde: str) -> bool:
    """Lee boletos de Cocos desde el Drive de descargas brutas."""
    exit_code = _run(["python3", "upload_cocos_drive.py", "--desde", desde], "2/4 cocos")
    if exit_code != 0:
        send_alarm(f"*upload_cocos_drive falló* (exit={exit_code}) desde {desde}")
        return False
    return True


def phase_cobros_fce(desde: str, hasta: str) -> bool:
    """Detecta cobros FCE-eCheq en la vista monetaria de Allaria y dispara webhooks."""
    exit_code = _run(
        ["python3", "run_allaria_cobros_fce.py", "--desde", desde, "--hasta", hasta],
        "3/5 cobros_fce",
    )
    if exit_code != 0:
        send_alarm(f"*run_allaria_cobros_fce falló* (exit={exit_code}) rango {desde} → {hasta}")
        return False
    return True


def phase_zapier(desde: str, hasta: str) -> bool:
    """Dispara webhooks Zapier para las fechas recientes."""
    exit_code = _run(
        ["python3", "run_boletos_zapier.py", desde, hasta],
        "4/5 zapier",
    )
    if exit_code != 0:
        send_alarm(
            f"*run_boletos_zapier tuvo errores* (exit={exit_code})\n"
            f"Rango: {desde} → {hasta}. Algunas fechas pueden no estar en Jira."
        )
        return False
    return True


def phase_verify(fechas: list[str]) -> int:
    """
    Verifica local vs Jira para cada fecha.
    Publica resumen en Slack y alarma si hay faltantes.
    Retorna el total de faltantes.
    """
    logger = logging.getLogger("orchestrator")
    logger.info("─" * 55)
    logger.info("[5/5 verify] Verificando %d fechas en Jira", len(fechas))

    total_faltantes = 0

    for fecha in fechas:
        try:
            resultado = verify_fecha(fecha)
        except Exception as e:
            logger.error("[verify] Error en %s: %s", fecha, e)
            send_alarm(f"Error verificando Jira para *{fecha}*: `{e}`")
            continue

        # Publicar resumen visual (tabla PDF vs Jira)
        if resultado["local_count"] > 0 or resultado["jira_issue_count"] > 0:
            send_resumen_fecha(fecha, resultado)

        faltantes = resultado.get("faltantes", [])
        if not faltantes:
            continue

        total_faltantes += len(faltantes)
        lineas = "\n".join(
            f"  • {folder} / {tipo}  nro={nro}"
            for folder, tipo, nro in faltantes
        )
        send_alarm(
            f"*{fecha}* — {len(faltantes)} boleto(s) en Drive sin issue en Jira:\n{lineas}"
        )

    # Verificar cobros FCE — deben figurar como "Cobrada" en Jira
    for fecha in fechas:
        try:
            pendientes = verify_cobros_fecha(fecha)
        except Exception as e:
            logger.error("[verify cobros] Error en %s: %s", fecha, e)
            send_alarm(f"Error verificando cobros FCE en Jira para *{fecha}*: `{e}`")
            continue

        if pendientes:
            lineas = "\n".join(
                f"  • fce={c['fce']}  nro={c['nro_boleto']}  status={c.get('jira_status')}"
                for c in pendientes
            )
            send_alarm(
                f"*{fecha}* — {len(pendientes)} cobro(s) FCE sin status 'Cobrada' en Jira:\n{lineas}"
            )

    return total_faltantes


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    hoy = date.today().isoformat()
    _setup_logging(hoy)
    logger = logging.getLogger("orchestrator")

    logger.info("=" * 55)
    logger.info("daily_orchestrator  %s", hoy)
    logger.info("=" * 55)

    send_info(f"⏳ Iniciando procesamiento diario — {hoy}")

    errores: list[str] = []

    # Phase 1 — Download
    if not phase_download():
        errores.append("batch_download")

    # Phase 2 — Cocos (desde hace 2 días para cubrir el fin de semana)
    desde_cocos = (date.today() - timedelta(days=2)).isoformat()
    if not phase_cocos(desde_cocos):
        errores.append("cocos")

    # Rango de fechas recientes para Cobros FCE + Zapier + verificación
    dias_recientes = _recent_business_days(VENTANA_DIAS)
    desde = dias_recientes[0]
    hasta = dias_recientes[-1]
    logger.info("Ventana: %s → %s (%d días)", desde, hasta, len(dias_recientes))

    # Phase 3 — Cobros FCE (Allaria monetaria)
    if not phase_cobros_fce(desde, hasta):
        errores.append("cobros_fce")

    # Phase 4 — Zapier
    if not phase_zapier(desde, hasta):
        errores.append("zapier")

    # Phase 5 — Verificación Jira
    fechas = _bdays(date.fromisoformat(desde), date.fromisoformat(hasta))
    total_faltantes = phase_verify(fechas)

    # Resumen final
    if errores or total_faltantes > 0:
        resumen = (
            f"⚠️ *Procesamiento {hoy} — con advertencias*\n"
            f"  Fases con error: {', '.join(errores) if errores else 'ninguna'}\n"
            f"  Boletos sin Jira: {total_faltantes}"
        )
    else:
        resumen = f"✅ *Procesamiento {hoy} — todo OK*"

    send_info(resumen)
    logger.info(resumen)

    return 0 if not errores else 1


if __name__ == "__main__":
    sys.exit(main())

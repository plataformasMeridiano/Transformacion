#!/bin/bash
# run_daily.sh — Entry point del cron diario de boletos.
#
# Solo lanza daily_orchestrator.py, que maneja todas las fases,
# errores y notificaciones Slack internamente.
#
# Cron (lunes a sábado, 9:00 AM Argentina = 12:00 UTC):
#   0 12 * * 1-6 cd /ruta/DescargaBoletos && xvfb-run --auto-servernum \
#     python3 daily_orchestrator.py >> logs/cron.log 2>&1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p logs

exec xvfb-run --auto-servernum python3 daily_orchestrator.py

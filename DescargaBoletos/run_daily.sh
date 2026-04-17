#!/bin/bash
# run_daily.sh — Descarga diaria de boletos + procesamiento Zapier/Jira
#
# Cron: lunes a viernes a las 9:00 AM Argentina (UTC-3 = 12:00 UTC)
#   0 12 * * 1-5 /mnt/c/dev/Meridiano/Transformacion/DescargaBoletos/run_daily.sh

SCRIPT_DIR="/mnt/c/dev/Meridiano/Transformacion/DescargaBoletos"
LOG_DIR="$SCRIPT_DIR/logs"
DATE=$(date +%Y-%m-%d)
LOG="$LOG_DIR/daily_${DATE}.log"

mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR"

echo "===============================" >> "$LOG"
echo "run_daily.sh — $(date)" >> "$LOG"
echo "===============================" >> "$LOG"

# Últimos 2 días hábiles
AYER=$(date -d "yesterday" +%Y-%m-%d)
ANTES=$(date -d "2 days ago" +%Y-%m-%d)

# 1. Descarga de boletos (últimos 2 días, xvfb-run para scrapers headless=false)
echo "[1/2] Descargando boletos $ANTES → $AYER..." | tee -a "$LOG"
xvfb-run --auto-servernum python3 batch_download.py "$ANTES" "$AYER" >> "$LOG" 2>&1
EXIT1=$?
echo "batch_download exit=$EXIT1" | tee -a "$LOG"

# 2. Boletos Cocos Capital (desde Drive fuente, últimos 7 días)
echo "[2/3] Cocos Drive desde $ANTES..." | tee -a "$LOG"
python3 upload_cocos_drive.py --desde "$ANTES" >> "$LOG" 2>&1
EXIT2=$?
echo "cocos_drive exit=$EXIT2" | tee -a "$LOG"

# 3. Procesamiento Zapier → Jira (desde hace 2 días)
echo "[3/3] Zapier desde $ANTES..." | tee -a "$LOG"
python3 run_boletos_zapier.py "$ANTES" >> "$LOG" 2>&1
EXIT3=$?
echo "zapier exit=$EXIT3" | tee -a "$LOG"

echo "===============================" >> "$LOG"
echo "FIN — $(date)" >> "$LOG"
echo "===============================" >> "$LOG"

[ $EXIT1 -eq 0 ] && [ $EXIT2 -eq 0 ] && [ $EXIT3 -eq 0 ]

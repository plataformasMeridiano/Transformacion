#!/bin/bash
# sync_cheques_novedades.sh — disparo periódico de sync-cmf-cheques en modo delta.
#
# Trae de CMF/COELSA lo que cambió desde la marca de agua, lo sincroniza en
# Supabase, detecta las novedades de estado y drena la cola de webhooks.
#
# Cron en la VM (días hábiles, 08:00 a 18:00 hora Argentina — la VM ya está en
# America/Argentina/Buenos_Aires, así que los horarios se escriben en local):
#   */15 8-17 * * 1-5  ~/Transformacion/Supabase/scripts/sync_cheques_novedades.sh
#   0 18 * * 1-5       ~/Transformacion/Supabase/scripts/sync_cheques_novedades.sh
#
# sinEstado=true es OBLIGATORIO: filtrando por ACTIVO nunca veríamos el pase a
# DEPOSITADO, que es justamente la novedad que se quiere detectar.
#
# NO se pasa crearJira: el alta de issues nuevos es una decisión aparte y se
# dispara explícitamente. Esta corrida solo sigue lo que ya está en cartera.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ENV_FILE="${ENV_FILE:-$HOME/Transformacion/DescargaBoletos/.env}"
LOG_DIR="${LOG_DIR:-$HOME/Transformacion/DescargaBoletos/logs}"
LOG="$LOG_DIR/cheques_novedades.log"
LOCK="/tmp/sync_cheques_novedades.lock"

mkdir -p "$LOG_DIR"

# flock impide que dos corridas se pisen. La ventana es de 15 min y una corrida
# tarda ~20 s, pero si CMF se pone lento el isolate llega a 150 s y conviene que
# la siguiente se saltee en vez de encimarse. -n = si está tomado, salir.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') SALTEADA: hay otra corrida en curso" >> "$LOG"
  exit 0
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "$(date '+%F %T') ERROR: no existe $ENV_FILE" >> "$LOG"
  exit 1
fi

# Se leen con python y no con grep/cut/tr: el .env tiene claves con comillas y
# caracteres que rompen el quoting de shell, y `source` sobre un .env ajeno es
# peor todavía porque ejecuta lo que haya adentro.
leer_env() {
  python3 - "$ENV_FILE" "$1" <<'PY'
import sys
clave = sys.argv[2]
for linea in open(sys.argv[1], encoding="utf-8"):
    linea = linea.strip()
    if linea.startswith(clave + "=") and not linea.startswith("#"):
        print(linea.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
}

SUPABASE_URL=$(leer_env SUPABASE_URL)
SUPABASE_KEY=$(leer_env SUPABASE_KEY)

if [[ -z "$SUPABASE_URL" || -z "$SUPABASE_KEY" ]]; then
  echo "$(date '+%F %T') ERROR: faltan SUPABASE_URL / SUPABASE_KEY en $ENV_FILE" >> "$LOG"
  exit 1
fi

INICIO=$(date '+%F %T')
# --max-time 200: el isolate corta a los 150 s y devuelve 504; se le da aire para
# que llegue esa respuesta en vez de que curl corte antes y no quede registrada.
RESP=$(curl -sS -X POST "$SUPABASE_URL/functions/v1/sync-cmf-cheques" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUPABASE_KEY" \
  -d '{"delta":true,"sinEstado":true}' \
  --max-time 200 -w '\n__HTTP__%{http_code}' 2>&1)

HTTP=$(printf '%s' "$RESP" | sed -n 's/.*__HTTP__\([0-9]*\)$/\1/p')
CUERPO=$(printf '%s' "$RESP" | sed 's/__HTTP__[0-9]*$//')

# Resumen de una línea. El cuerpo entero solo cuando algo salió mal: si no, el
# log se vuelve ilegible a 40 corridas por día.
RESUMEN=$(printf '%s' "$CUERPO" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("(respuesta no-JSON)"); raise SystemExit
if not d.get("ok"):
    print("ERROR " + str(d.get("error"))[:200]); raise SystemExit
env = d.get("envio_novedades") or {}
print("traidos={} novedades={} nuevas={} enviadas={} fallidas={} sin_destino={} marca={} ms={}".format(
    d.get("traidos"), d.get("novedades_detectadas"), d.get("novedades_nuevas"),
    env.get("enviadas"), env.get("fallidas"), env.get("sin_destino"),
    d.get("marca_avanzada_a"), d.get("ms")))
' 2>/dev/null)

if [[ "$HTTP" == "200" && "$RESUMEN" != ERROR* ]]; then
  echo "$INICIO OK   $RESUMEN" >> "$LOG"
else
  echo "$INICIO FALLA http=$HTTP $RESUMEN" >> "$LOG"
  echo "    $(printf '%s' "$CUERPO" | head -c 600)" >> "$LOG"
fi

# ── PUENTE TEMPORAL ───────────────────────────────────────────────────────────
# El flujo definitivo es webhook por tipo. Mientras esos webhooks no existan, las
# novedades quedan en la cola y el issue de Jira no se entera de nada. Este paso
# las drena moviendo el issue con las transiciones provisorias del workflow.
#
# CUANDO LOS WEBHOOKS ESTEN ACTIVOS, BORRAR ESTE BLOQUE. La columna
# cheques_novedades.aplicado_por deja registrado qué se movió por acá.
if [[ -x "$SCRIPT_DIR/aplicar_transiciones_cheques.py" ]]; then
  TRANS=$("$SCRIPT_DIR/aplicar_transiciones_cheques.py" 2>&1)
  echo "$INICIO PUENTE $(printf '%s' "$TRANS" | tail -1)" >> "$LOG"
fi

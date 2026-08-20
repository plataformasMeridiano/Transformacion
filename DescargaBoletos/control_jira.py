"""
control_jira.py — Cierra el ciclo descarga → Jira.

Para los boletos descargados en un rango (por defecto, desde el último control
exitoso hasta hoy — así un día fallado se recupera en la corrida siguiente):

  1. Busca en Jira el issue que le corresponde a cada boleto por (nro_boleto, ALyC),
     mirando PAS y ACT — las cauciones colocadoras se cargan en ACT como Débito Bursátil.
  2. Guarda el `jira_issue_key` en `procesamiento_boletos`.
  3. Los que no aparecen: agrupa por (alyc, fecha_operacion) — que es la granularidad
     que acepta el webhook, no se puede reprocesar un boleto suelto — dispara el Zap,
     espera y vuelve a buscar.
  4. Lo que siga faltando se reporta para alertar.

Se apoya en `procesamiento_boletos`, no en el disco: así no depende de que la ALyC
esté en FOLDER_TO_JIRA (ese filtro dejó a IEB fuera de toda verificación por semanas).

Uso:
    python3 control_jira.py                      # desde el último checkpoint (deja checkpoint)
    python3 control_jira.py 2026-08-11            # un día puntual
    python3 control_jira.py 2026-08-01 2026-08-12 # rango
    python3 control_jira.py --dry                 # sin reprocesar ni escribir
"""
from __future__ import annotations

import logging
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone

from jira_controller import FOLDER_TO_JIRA, jira_search
from supabase_logger import (get_boletos_sin_jira, marcar_boleto_jira, sumar_reproceso,
                             ultima_corrida_control, registrar_corrida_control)

logger = logging.getLogger("control_jira")

# Hay DOS webhooks según el tipo de operación. Mandar un FCE al de cauciones
# cierra con "Fin Proceso" sin crear nada — parece que funcionó y no hizo nada.
_WEBHOOK_CAUCIONES = "https://hooks.zapier.com/hooks/catch/24963922/uqqfupo/"
_WEBHOOK_FCE       = "https://hooks.zapier.com/hooks/catch/24963922/ujlo78k/"
_TIPOS_FCE = {"Venta FCE-eCheq"}

# Tipos que HOY se descargan pero no se procesan en Zapier: no se reprocesan ni
# se reportan como faltantes. Títulos tiene su propio webhook, todavía en pruebas.
# Cuando entre en producción, sacarlo de acá.
_TIPOS_SIN_PROCESO = {"Títulos"}

_ESPERA_ENTRE_DISPAROS = 180   # s entre nuestros propios disparos, para no encimar corridas

# El control NO espera el resultado del reproceso: el Zap tarda varios minutos por
# fecha y con muchas ALyCs esperar sería frágil. Dispara una vez, deja el boleto
# marcado (reproceso_intentos > 0) y lo confirma en la corrida siguiente — el cron
# corre dos veces por día, así que la confirmación llega a las pocas horas.
MAX_REPROCESOS = 3   # después de esto se deja de disparar y se alerta


def _webhook_de(tipo: str) -> str:
    return _WEBHOOK_FCE if tipo in _TIPOS_FCE else _WEBHOOK_CAUCIONES

CF_NRO  = "customfield_10807"
CF_ALYC = "customfield_11360"


MAX_DIAS_ATRAS = 15   # tope de recuperación, para que un checkpoint muy viejo no
                      # dispare cientos de reprocesos de golpe


def _rango_por_defecto() -> tuple[str, str]:
    """Rango a revisar: desde el último checkpoint hasta las 00:00 UTC de hoy.

    Retomar desde el checkpoint —y no desde ayer— hace que un día fallado se
    recupere solo en la corrida siguiente. Si nunca corrió, arranca en ayer.
    """
    a_ts = lambda d: datetime.combine(d, dtime.min, tzinfo=timezone.utc).isoformat()
    hoy = date.today()
    hasta = a_ts(hoy)

    ultimo = ultima_corrida_control()
    if not ultimo:
        logger.info("Sin corridas previas: se revisa solo el día de ayer.")
        return a_ts(hoy - timedelta(days=1)), hasta

    desde_dt = datetime.fromisoformat(ultimo.replace("Z", "+00:00"))
    tope = datetime.combine(hoy - timedelta(days=MAX_DIAS_ATRAS), dtime.min, tzinfo=timezone.utc)
    if desde_dt < tope:
        logger.warning("El último control fue el %s; se recorta la recuperación a %d días.",
                       desde_dt.date(), MAX_DIAS_ATRAS)
        desde_dt = tope

    dias = (hoy - desde_dt.date()).days
    if dias > 1:
        logger.info("Último control hasta %s: se recuperan %d días.", desde_dt.date(), dias)
    return desde_dt.isoformat(), hasta


def _buscar_en_jira(nros: list[str]) -> dict[str, tuple[str, str]]:
    """nro_boleto → (issue_key, alyc_jira). Consulta PAS y ACT en lotes."""
    encontrados: dict[str, tuple[str, str]] = {}
    LOTE = 80
    for i in range(0, len(nros), LOTE):
        chunk = [n for n in nros[i:i + LOTE] if str(n).isdigit()]
        if not chunk:
            continue
        jql = f"cf[10807] in ({', '.join(chunk)})"
        try:
            for issue in jira_search(jql, [CF_NRO, CF_ALYC]):
                f = issue["fields"]
                nro = f.get(CF_NRO)
                if nro is None:
                    continue
                alyc_jira = (f.get(CF_ALYC) or "").split(",")[0].strip()
                encontrados[str(int(nro))] = (issue["key"], alyc_jira)
        except Exception as exc:
            logger.error("Búsqueda en Jira falló para un lote: %s", exc)
    return encontrados


def _disparar(alyc_jira: str, fecha: str, tipo: str) -> bool:
    base = _webhook_de(tipo)
    url = f"{base}?{urllib.parse.urlencode({'fecha': fecha, 'alyc': alyc_jira})}"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=30) as resp:
            logger.info("[reproceso] %s / %s → HTTP %s", alyc_jira, fecha, resp.status)
            return True
    except Exception as exc:
        logger.error("[reproceso] %s / %s falló: %s", alyc_jira, fecha, exc)
        return False


def _conciliar(boletos: list[dict], dry: bool) -> list[dict]:
    """Marca los que ya tienen issue. Retorna los que siguen sin aparecer."""
    if not boletos:
        return []
    hallados = _buscar_en_jira([b["nro_boleto"] for b in boletos])
    faltantes = []
    for b in boletos:
        match = hallados.get(str(b["nro_boleto"]))
        if not match:
            faltantes.append(b)
            continue
        key, alyc_jira = match
        esperado = FOLDER_TO_JIRA.get(b["alyc"], b["alyc"])
        if alyc_jira and alyc_jira != esperado:
            # Mismo número de boleto en otra ALyC: no es el issue de este boleto.
            logger.warning("Boleto %s: %s es de '%s' y se esperaba '%s' — no se asocia",
                           b["nro_boleto"], key, alyc_jira, esperado)
            faltantes.append(b)
            continue
        logger.info("OK  %-12s %-22s %-9s → %s",
                    b["alyc"], b["tipo"], b["nro_boleto"], key)
        if not dry:
            marcar_boleto_jira(b["id"], key)
    return faltantes


def _checkpoint(activo: bool, desde: str, hasta: str, r: dict) -> None:
    """Deja el punto de control. Solo en corridas automáticas: si se pide un rango a
    mano no hay que mover el checkpoint, o se saltearían días sin revisar."""
    if not activo:
        return
    registrar_corrida_control(desde, hasta, r["revisados"], r["resueltos"],
                              len(r["faltantes"]), r.get("omitidos", 0))


def controlar(desde: str, hasta: str | None = None, dry: bool = False,
              checkpoint: bool = False) -> dict:
    """`checkpoint=True` deja registrada la corrida para que la próxima retome desde acá."""
    todos = get_boletos_sin_jira(desde, hasta)

    pendientes = [b for b in todos if b["tipo"] not in _TIPOS_SIN_PROCESO]
    omitidos = len(todos) - len(pendientes)
    if omitidos:
        logger.info("Omitidos %d boleto(s) de tipos que aún no se procesan en Zapier (%s)",
                    omitidos, ", ".join(sorted(_TIPOS_SIN_PROCESO)))

    logger.info("Boletos sin issue de Jira en el rango: %d", len(pendientes))
    if not pendientes:
        r = {"revisados": 0, "resueltos": 0, "faltantes": [], "en_curso": [],
             "omitidos": omitidos}
        _checkpoint(checkpoint and not dry, desde, hasta, r)
        return r

    # 1ª pasada: puede que ya tengan issue y solo falte registrarlo.
    faltantes = _conciliar(pendientes, dry)
    resueltos = len(pendientes) - len(faltantes)
    logger.info("Ya tenían issue: %d — siguen sin issue: %d", resueltos, len(faltantes))

    if not faltantes or dry:
        r = {"revisados": len(pendientes), "resueltos": resueltos,
             "faltantes": faltantes, "en_curso": [], "omitidos": omitidos}
        _checkpoint(checkpoint and not dry, desde, hasta, r)
        return r

    # Los que ya agotaron los intentos no se vuelven a disparar: se reportan.
    agotados  = [b for b in faltantes if (b.get("reproceso_intentos") or 0) >= MAX_REPROCESOS]
    a_disparar = [b for b in faltantes if (b.get("reproceso_intentos") or 0) < MAX_REPROCESOS]
    if agotados:
        logger.warning("%d boleto(s) sin issue tras %d reprocesos — no se reintentan",
                       len(agotados), MAX_REPROCESOS)

    # Reprocesar: el webhook acepta (fecha, alyc), no un boleto suelto; y el de FCE
    # es distinto al de cauciones, así que el grupo incluye a cuál pegarle.
    grupos: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for b in a_disparar:
        canal = "FCE" if b["tipo"] in _TIPOS_FCE else "Cauciones"
        grupos[(b["alyc"], b["fecha_operacion"], canal)].append(b)
    logger.info("Reprocesando %d grupo(s) (alyc, fecha, canal)", len(grupos))

    for i, ((alyc, fecha, canal), boletos) in enumerate(sorted(grupos.items()), 1):
        alyc_jira = FOLDER_TO_JIRA.get(alyc, alyc)
        logger.info("[%d/%d] %s / %s / %s — %d boleto(s)",
                    i, len(grupos), alyc_jira, fecha, canal, len(boletos))
        if _disparar(alyc_jira, fecha, boletos[0]["tipo"]):
            for b in boletos:
                sumar_reproceso(b["id"], b.get("reproceso_intentos", 0))
        if i < len(grupos):
            time.sleep(_ESPERA_ENTRE_DISPAROS)

    # Sin espera: lo disparado se confirma en la corrida siguiente.
    logger.info("Reproceso disparado. Se verifica en la próxima corrida.")
    r = {
        "revisados":  len(pendientes),
        "resueltos":  resueltos,
        "faltantes":  agotados,      # sólo lo que ya agotó reintentos alerta
        "en_curso":   a_disparar,    # disparado ahora, pendiente de confirmar
        "omitidos":   omitidos,
    }
    _checkpoint(checkpoint, desde, hasta, r)
    return r


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv

    if not args:
        desde, hasta = _rango_por_defecto()
    elif len(args) == 1:
        d = date.fromisoformat(args[0])
        desde = datetime.combine(d, dtime.min, tzinfo=timezone.utc).isoformat()
        hasta = datetime.combine(d + timedelta(days=1), dtime.min, tzinfo=timezone.utc).isoformat()
    else:
        desde = datetime.combine(date.fromisoformat(args[0]), dtime.min, tzinfo=timezone.utc).isoformat()
        hasta = datetime.combine(date.fromisoformat(args[1]) + timedelta(days=1),
                                 dtime.min, tzinfo=timezone.utc).isoformat()

    # Solo la corrida automática (sin fechas por CLI) mueve el checkpoint.
    automatica = not args
    logger.info("Control de descargas entre %s y %s%s", desde, hasta, "  [DRY-RUN]" if dry else "")
    r = controlar(desde, hasta, dry, checkpoint=automatica)

    logger.info("=" * 60)
    logger.info("Revisados: %d   con issue: %d   reprocesados: %d   sin issue tras %d intentos: %d",
                r["revisados"], r["resueltos"], len(r.get("en_curso", [])),
                MAX_REPROCESOS, len(r["faltantes"]))
    for b in r.get("en_curso", []):
        logger.info("EN CURSO   %-12s %-22s %-9s  (operación %s)",
                    b["alyc"], b["tipo"], b["nro_boleto"], b["fecha_operacion"])
    for b in r["faltantes"]:
        logger.warning("SIN ISSUE  %-12s %-22s %-9s  (operación %s)",
                       b["alyc"], b["tipo"], b["nro_boleto"], b["fecha_operacion"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

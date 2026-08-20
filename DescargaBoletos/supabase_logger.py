"""
supabase_logger.py — Logging a Supabase.

Tablas:
  procesamiento_boletos  — un registro por PDF descargado
  corridas               — maestro: una fila por ejecución de main.py
  corridas_detalle       — detalle: una fila por ALYC por ejecución
"""
import json
import logging
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TABLE_BOLETOS   = "procesamiento_boletos"
_TABLE_DESCARGAS = "procesamiento_boletos_descargas"
_TABLE_CONTROL   = "control_jira_corridas"
_TABLE_CORRIDAS  = "descargas_cauciones_corridas_log"
_TABLE_DETALLE   = "descargas_cauciones_corridas_detalle_log"

# Clave natural de procesamiento_boletos (índice único) — para el upsert.
_BOLETOS_CONFLICT = "alyc,tipo,nro_boleto,fecha_operacion"


def _get_client() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise EnvironmentError("Faltan SUPABASE_URL o SUPABASE_KEY en el entorno")
    return url, key


def _post(table: str, payload: dict, return_rep: bool = False) -> dict | None:
    """INSERT en una tabla. Si return_rep=True, retorna el registro insertado."""
    try:
        url, key = _get_client()
        endpoint = f"{url}/rest/v1/{table}"
        data = json.dumps(payload).encode()
        prefer = "return=representation" if return_rep else "return=minimal"
        req = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
                "Prefer":        prefer,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if return_rep:
                body = json.loads(resp.read())
                return body[0] if isinstance(body, list) else body
        return {}
    except Exception as exc:
        logger.warning("Supabase POST falló [%s]: %s", table, exc)
        return None


def _upsert(table: str, payload: dict, on_conflict: str) -> dict | None:
    """INSERT ... ON CONFLICT DO UPDATE sobre `on_conflict`. Retorna el registro resultante.

    `merge-duplicates` sólo pisa las columnas presentes en el payload, así que omitir
    una columna (ej. drive_file_id cuando todavía no hay) conserva el valor guardado.
    """
    try:
        url, key = _get_client()
        endpoint = f"{url}/rest/v1/{table}?on_conflict={on_conflict}"
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates,return=representation",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return body[0] if isinstance(body, list) and body else body
    except Exception as exc:
        logger.warning("Supabase UPSERT falló [%s]: %s", table, exc)
        return None


def _patch(table: str, record_id: str, payload: dict) -> bool:
    """UPDATE de un registro por id."""
    try:
        url, key = _get_client()
        endpoint = f"{url}/rest/v1/{table}?id=eq.{record_id}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            endpoint,
            data=data,
            method="PATCH",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception as exc:
        logger.warning("Supabase PATCH falló [%s/%s]: %s", table, record_id, exc)
        return False


# ── procesamiento_boletos ─────────────────────────────────────────────────────

def log_boleto(
    fecha_operacion: str,
    alyc: str,
    tipo: str,
    nro_boleto: str,
    filename: str,
    drive_file_id: str | None = None,
) -> str | None:
    """
    Registra un boleto descargado. Retorna el id del registro, o None si falló.

    `procesamiento_boletos` tiene UNA fila por boleto (unique alyc+tipo+nro+fecha_operacion),
    así que esto es un **upsert**: el cron re-descarga los últimos días hábiles y sin esto
    la tabla acumulaba ~5 filas por boleto.

    Cada descarga deja además una fila en `procesamiento_boletos_descargas` (auditoría).

    Llamar con drive_file_id=None tras la descarga local; luego update_boleto_drive() tras subir.
    """
    ahora = datetime.now(timezone.utc).isoformat()
    fila = {
        "fecha_operacion": fecha_operacion,
        "alyc":            alyc,
        "tipo":            tipo,
        "nro_boleto":      str(nro_boleto),
        "filename":        filename,
        "fecha_descarga":  ahora,
    }
    # No pisar con NULL un drive_file_id ya guardado: en el upsert solo se manda
    # la columna cuando hay valor.
    if drive_file_id:
        fila["drive_file_id"] = drive_file_id

    result = _upsert(_TABLE_BOLETOS, fila, on_conflict=_BOLETOS_CONFLICT)
    boleto_id = result.get("id") if result else None

    if boleto_id:
        _post(_TABLE_DESCARGAS, {
            "boleto_id":       boleto_id,
            "fecha_operacion": fecha_operacion,
            "alyc":            alyc,
            "tipo":            tipo,
            "nro_boleto":      str(nro_boleto),
            "filename":        filename,
            "drive_file_id":   drive_file_id,
            "fecha_descarga":  ahora,
        })
    return boleto_id


def update_boleto_drive(boleto_id: str, drive_file_id: str) -> bool:
    """Actualiza drive_file_id de un registro ya insertado."""
    return _patch(_TABLE_BOLETOS, boleto_id, {"drive_file_id": drive_file_id})


def get_boletos_sin_drive() -> list[dict]:
    """
    Retorna registros de procesamiento_boletos con drive_file_id IS NULL.
    Cada elemento tiene: id, fecha_operacion, alyc, tipo, nro_boleto, filename.
    """
    try:
        url, key = _get_client()
        req = urllib.request.Request(
            f"{url}/rest/v1/{_TABLE_BOLETOS}"
            "?drive_file_id=is.null"
            "&select=id,fecha_operacion,alyc,tipo,nro_boleto,filename",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Accept":        "application/json",
                "Range":         "0-9999",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("get_boletos_sin_drive falló: %s", exc)
        return []


def get_boletos_sin_jira(desde: str, hasta: str | None = None) -> list[dict]:
    """Boletos sin issue de Jira: los de la ventana MÁS los que quedaron pendientes.

    `desde`/`hasta` filtran por `fecha_descarga`, no por fecha de operación.

    Además de la ventana se arrastran los que ya tienen `reproceso_intentos > 0`:
    el control dispara el reproceso y NO espera el resultado — lo confirma en la
    corrida siguiente. Sin esto, al avanzar el checkpoint quedarían fuera de vista.
    """
    try:
        url, key = _get_client()
        # El "+" del offset horario se interpreta como espacio en una query string:
        # hay que escaparlo o PostgREST responde 400.
        _q = lambda v: urllib.parse.quote(v, safe="")
        ventana = (f"and(fecha_descarga.gte.{_q(desde)},fecha_descarga.lt.{_q(hasta)})"
                   if hasta else f"fecha_descarga.gte.{_q(desde)}")
        filtros = [
            "jira_issue_key=is.null",
            f"or=({ventana},reproceso_intentos.gt.0)",
            "select=id,fecha_operacion,alyc,tipo,nro_boleto,filename,drive_file_id,reproceso_intentos",
            "order=alyc.asc,fecha_operacion.asc,nro_boleto.asc",
        ]
        req = urllib.request.Request(
            f"{url}/rest/v1/{_TABLE_BOLETOS}?" + "&".join(filtros),
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Accept":        "application/json",
                "Range":         "0-9999",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("get_boletos_sin_jira falló: %s", exc)
        return []


def marcar_boleto_jira(boleto_id: str, jira_issue_key: str) -> bool:
    """Guarda el issue de Jira que le corresponde al boleto y sella la verificación."""
    return _patch(_TABLE_BOLETOS, boleto_id, {
        "jira_issue_key":     jira_issue_key,
        "jira_verificado_at": datetime.now(timezone.utc).isoformat(),
    })


def ultima_corrida_control() -> str | None:
    """`hasta` de la última corrida exitosa del control, o None si nunca corrió."""
    try:
        url, key = _get_client()
        req = urllib.request.Request(
            f"{url}/rest/v1/{_TABLE_CONTROL}"
            "?ok=is.true&select=hasta&order=hasta.desc&limit=1",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Accept":        "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            filas = json.loads(resp.read())
            return filas[0]["hasta"] if filas else None
    except Exception as exc:
        logger.warning("ultima_corrida_control falló: %s", exc)
        return None


def registrar_corrida_control(desde: str, hasta: str, revisados: int, resueltos: int,
                              faltantes: int, omitidos: int, ok: bool = True) -> None:
    """Deja el checkpoint para que la próxima corrida sepa desde dónde retomar."""
    _post(_TABLE_CONTROL, {
        "desde": desde, "hasta": hasta,
        "revisados": revisados, "resueltos": resueltos,
        "faltantes": faltantes, "omitidos": omitidos, "ok": ok,
    })


def sumar_reproceso(boleto_id: str, intentos_actuales: int) -> bool:
    """Incrementa el contador de reprocesos disparados para ese boleto."""
    return _patch(_TABLE_BOLETOS, boleto_id, {
        "reproceso_intentos": (intentos_actuales or 0) + 1,
    })


# ── corridas (maestro) ────────────────────────────────────────────────────────

def start_corrida(
    fecha_procesada: str,
    alycs: list[str] | None = None,
    tipo_boleto: list[str] | None = None,
) -> str | None:
    """
    Inserta una corrida en estado 'corriendo'.
    Retorna el id UUID o None si falló.
    """
    record = _post(_TABLE_CORRIDAS, {
        "fecha_procesada":   fecha_procesada,
        "alycs_solicitadas": alycs,
        "tipo_boleto":       tipo_boleto,
        "estado":            "corriendo",
        "fecha_inicio":      datetime.now(timezone.utc).isoformat(),
    }, return_rep=True)
    if record:
        return record.get("id")
    return None


def finish_corrida(
    corrida_id: str,
    total_desc: int,
    total_sub: int,
    total_err: int,
    estado: str = "completado",
    notas: str | None = None,
) -> bool:
    """Actualiza la corrida con totales y fecha_fin."""
    payload = {
        "fecha_fin":   datetime.now(timezone.utc).isoformat(),
        "estado":      estado,
        "total_desc":  total_desc,
        "total_sub":   total_sub,
        "total_err":   total_err,
    }
    if notas:
        payload["notas"] = notas
    return _patch(_TABLE_CORRIDAS, corrida_id, payload)


# ── corridas_detalle ──────────────────────────────────────────────────────────

def start_alyc_detalle(corrida_id: str, alyc: str, sistema: str) -> str | None:
    """
    Inserta un detalle de ALYC en estado 'corriendo'.
    Retorna el id UUID o None si falló.
    """
    record = _post(_TABLE_DETALLE, {
        "corrida_id":  corrida_id,
        "alyc":        alyc,
        "sistema":     sistema,
        "estado":      "corriendo",
        "fecha_inicio": datetime.now(timezone.utc).isoformat(),
    }, return_rep=True)
    if record:
        return record.get("id")
    return None


def finish_alyc_detalle(
    detalle_id: str,
    desc_count: int,
    sub_count: int,
    err_count: int,
    estado: str,
    error_detalle: str | None = None,
) -> bool:
    """Actualiza el detalle de ALYC con los resultados."""
    payload = {
        "fecha_fin":    datetime.now(timezone.utc).isoformat(),
        "estado":       estado,
        "desc_count":   desc_count,
        "sub_count":    sub_count,
        "err_count":    err_count,
    }
    if error_detalle:
        payload["error_detalle"] = error_detalle
    return _patch(_TABLE_DETALLE, detalle_id, payload)


# ── consultas de estado ───────────────────────────────────────────────────────

def get_fechas_completadas(active_alycs: list[str], desde: str) -> dict[str, set[str]]:
    """
    Retorna, para cada fecha >= desde, el conjunto de ALYCs que tienen al
    menos un detalle con estado='ok' en alguna corrida de esa fecha.

    Resultado: { "2026-04-17": {"Puente", "WIN", ...}, ... }

    Sirve para detectar qué fechas faltan: si el set de ALYCs para una fecha
    no es un superconjunto de active_alycs, esa fecha necesita reprocesarse.
    """
    try:
        url, key = _get_client()
        headers = {
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Accept":        "application/json",
            "Range":         "0-9999",   # hasta 10 000 filas
        }

        # ── Paso 1: corridas con fecha_procesada >= desde ─────────────────
        req = urllib.request.Request(
            f"{url}/rest/v1/{_TABLE_CORRIDAS}"
            f"?fecha_procesada=gte.{desde}&select=id,fecha_procesada",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            corridas = json.loads(resp.read())

        if not corridas:
            return {}

        corrida_map: dict[str, str] = {c["id"]: c["fecha_procesada"] for c in corridas}
        ids_csv = ",".join(corrida_map.keys())

        # ── Paso 2: detalles con estado='ok' para esas corridas ───────────
        req2 = urllib.request.Request(
            f"{url}/rest/v1/{_TABLE_DETALLE}"
            f"?corrida_id=in.({ids_csv})&estado=eq.ok&select=corrida_id,alyc",
            headers=headers,
        )
        with urllib.request.urlopen(req2, timeout=15) as resp:
            detalles = json.loads(resp.read())

        # ── Construir mapa fecha → {alycs ok} ────────────────────────────
        result: dict[str, set[str]] = {}
        for d in detalles:
            fecha = corrida_map.get(d["corrida_id"])
            if fecha:
                result.setdefault(fecha, set()).add(d["alyc"])

        return result

    except Exception as exc:
        logger.warning("get_fechas_completadas falló: %s", exc)
        return {}

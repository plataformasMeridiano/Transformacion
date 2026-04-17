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
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TABLE_BOLETOS  = "procesamiento_boletos"
_TABLE_CORRIDAS = "corridas"
_TABLE_DETALLE  = "corridas_detalle"


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
) -> bool:
    """Inserta un registro en procesamiento_boletos."""
    result = _post(_TABLE_BOLETOS, {
        "fecha_operacion": fecha_operacion,
        "alyc":            alyc,
        "tipo":            tipo,
        "nro_boleto":      str(nro_boleto),
        "filename":        filename,
        "drive_file_id":   drive_file_id,
        "fecha_descarga":  datetime.now(timezone.utc).isoformat(),
    })
    return result is not None


# ── corridas (maestro) ────────────────────────────────────────────────────────

def start_corrida(fecha_procesada: str, alycs: list[str] | None = None) -> str | None:
    """
    Inserta una corrida en estado 'corriendo'.
    Retorna el id UUID o None si falló.
    """
    record = _post(_TABLE_CORRIDAS, {
        "fecha_procesada":  fecha_procesada,
        "alycs_solicitadas": alycs,
        "estado":           "corriendo",
        "fecha_inicio":     datetime.now(timezone.utc).isoformat(),
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

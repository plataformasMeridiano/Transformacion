"""
supabase_logger.py — Registra cada PDF descargado en la tabla procesamiento_boletos.
"""
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TABLE = "procesamiento_boletos"


def _get_client() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise EnvironmentError("Faltan SUPABASE_URL o SUPABASE_KEY en el entorno")
    return url, key


def log_boleto(
    fecha_operacion: str,
    alyc: str,
    tipo: str,
    nro_boleto: str,
    filename: str,
    drive_file_id: str | None = None,
) -> bool:
    """
    Inserta un registro en procesamiento_boletos.
    Retorna True si fue exitoso, False si falló (no interrumpe el flujo principal).
    """
    try:
        url, key = _get_client()
        endpoint = f"{url}/rest/v1/{_TABLE}"
        payload = json.dumps({
            "fecha_operacion": fecha_operacion,
            "alyc":            alyc,
            "tipo":            tipo,
            "nro_boleto":      str(nro_boleto),
            "filename":        filename,
            "drive_file_id":   drive_file_id,
            "fecha_descarga":  datetime.now(timezone.utc).isoformat(),
        }).encode()
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
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
        logger.warning("Supabase log falló [%s/%s/%s]: %s", alyc, fecha_operacion, filename, exc)
        return False

"""
diag_conosur_fields.py — Verifica el matching de pases para una fecha.

Uso:
    python3 diag_conosur_fields.py [FECHA]   # default: 2026-01-28
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(".env"))

import logging
logging.basicConfig(level=logging.WARNING)

from scrapers.alyc_sistemaD import ConoSurScraper

FECHA = sys.argv[1] if len(sys.argv) > 1 else "2026-01-28"

ALYC_CONFIG = {
    "nombre": "ConoSur",
    "sistema": "sistemaD",
    "url_login": "https://virtualbroker-conosur.aunesa.com/auth/signin",
    "usuario": "${CONOSUR_USUARIO}",
    "contrasena": "${CONOSUR_PASSWORD}",
    "opciones": {
        "cuenta": "3003",
        "caucion_conceptos": ["Tomadora", "Colocadora"],
        "tipo_operacion": ["Cauciones", "Pases"],
        "timeout_ms": 30000,
    },
}
GENERAL_CONFIG = {"headless": True, "download_dir": "downloads"}

_URL_SESSION = "https://virtualbroker-conosur.aunesa.com/api/auth/session"
_API_BASE = "https://vb-back-conosur.aunesa.com/api"


async def main():
    fecha_dt = datetime.strptime(FECHA, "%Y-%m-%d")
    fecha_fmt = fecha_dt.strftime("%d/%m/%Y")
    fecha_hasta_fmt = (fecha_dt + timedelta(days=7)).strftime("%d/%m/%Y")

    async with ConoSurScraper(ALYC_CONFIG, GENERAL_CONFIG) as scraper:
        await scraper.login()
        page = scraper._page

        sess_resp = await page.context.request.get(_URL_SESSION)
        sess = await sess_resp.json()
        auth_h = {"Authorization": f"Bearer {sess['accessToken']}"}

        resp = await page.context.request.get(
            f"{_API_BASE}/v2/cuentas/3003/movimientos",
            params={
                "fechaDesde": fecha_fmt, "fechaHasta": fecha_hasta_fmt,
                "tipoMovimiento": "monetarios", "page": "1", "size": "500",
                "estado": "DIS", "especie": "ARS",
            },
            headers=auth_h,
        )
        data = await resp.json()
        all_movs = data.get("movimientos", {}).get("content", [])

        print(f"\n=== {len(all_movs)} movimientos en rango ===")
        for m in all_movs:
            print(f"  [{m.get('concertacion')} → liq:{m.get('liquidacion')}]"
                  f"  {m.get('concepto')!r:28}  {m.get('numeroComprobante'):20}"
                  f"  [{m.get('simboloLocal','')}]")

        print(f"\n=== Pases seleccionados por _match_pases ===")
        pases = scraper._match_pases(all_movs, fecha_fmt)
        for m in pases:
            print(f"  [{m.get('concertacion')} → liq:{m.get('liquidacion')}]"
                  f"  {m.get('concepto')!r:28}  {m.get('numeroComprobante'):20}"
                  f"  [{m.get('simboloLocal','')}]")
        print(f"  → {len(pases)} boletos de pases")


asyncio.run(main())

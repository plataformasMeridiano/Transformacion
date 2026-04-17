"""
diag_metro_movimientos.py — Lista todos los campos de los movimientos MetroCorp para
una fecha, mostrando en especial descripcionOperacion y otros campos de clasificación.

Uso:
    python3 diag_metro_movimientos.py [FECHA]   # default: 2026-01-27
"""
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("diag_metro")

from scrapers.alyc_sistemaF import MetroCorpScraper  # noqa: E402

FECHA = sys.argv[1] if len(sys.argv) > 1 else "2026-01-27"

with open(Path("config.json")) as f:
    _config = json.load(f)

ALYC_CONFIG = next(a for a in _config["alycs"] if a["nombre"] == "MetroCorp")
GENERAL_CONFIG = {"headless": False, "download_dir": "downloads"}


async def main():
    async with MetroCorpScraper(ALYC_CONFIG, GENERAL_CONFIG) as scraper:
        logger.info("Haciendo login en MetroCorp...")
        await scraper.login()

        fecha_dt  = datetime.strptime(FECHA, "%Y-%m-%d")
        fecha_fmt = fecha_dt.strftime("%d/%m/%Y")
        iso_date  = scraper._make_iso(fecha_fmt)

        cuentas = ALYC_CONFIG["opciones"].get("cuentas", [])
        for cuenta in cuentas:
            display_name = cuenta.get("display_name", "")
            cuenta_id    = cuenta.get("cuenta", "")
            id_env       = cuenta.get("id_environment", 0)
            nombre       = cuenta.get("nombre", "")

            if display_name:
                logger.info("Cambiando a ambiente '%s'...", display_name)
                await scraper._switch_environment(display_name, 30_000)

            from scrapers.alyc_sistemaF import _URL_METROCORP
            await scraper._page.goto(_URL_METROCORP, wait_until="networkidle", timeout=30_000)
            await scraper._page.wait_for_timeout(2000)

            resp = await scraper._api_post("metrocorp.list", {
                "optionSelected": "movements",
                "principalAccount": cuenta_id,
                "species": "all",
                "date":     iso_date,
                "dateFrom": iso_date,
                "dateTo":   iso_date,
                "page": 1,
                "idEnvironment": id_env,
                "lang": "es",
                "channel": "frontend",
            })

            movements = resp.get("data", {}).get("movements", [])
            print(f"\n{'='*70}")
            print(f"CUENTA: {nombre}  ({cuenta_id})  — fecha: {FECHA}")
            print(f"Total movimientos: {len(movements)}")
            print(f"{'='*70}")

            for i, m in enumerate(movements):
                desc = m.get("descripcionOperacion", "")
                tipo_clasificado = scraper._classify(desc)
                nro  = m.get("numeroBoleto", "")
                ref  = m.get("referenciaMinuta", "")
                espec = m.get("codEspe", "")
                importe = m.get("importeNeto", "")
                print(f"\n  [{i}] CLASIFICADO: {tipo_clasificado}")
                print(f"      descripcionOperacion: {desc!r}")
                print(f"      numeroBoleto:         {nro!r}")
                print(f"      referenciaMinuta:     {ref!r}")
                print(f"      codEspe:              {espec!r}")
                print(f"      importeNeto:          {importe!r}")
                # Mostrar todos los campos para referencia
                otros = {k: v for k, v in m.items()
                         if k not in ("descripcionOperacion", "numeroBoleto",
                                      "referenciaMinuta", "codEspe", "importeNeto")}
                if otros:
                    print(f"      otros campos: {json.dumps(otros, ensure_ascii=False)[:300]}")


asyncio.run(main())

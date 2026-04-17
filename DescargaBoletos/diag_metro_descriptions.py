"""
diag_metro_descriptions.py

Llama a metrocorp.list para una fecha y muestra los campos clave de cada
movimiento (descripcionOperacion, numeroBoleto, referenciaMinuta) sin descargar PDFs.

Uso:
    python3 diag_metro_descriptions.py 2026-02-02
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from batch_download import _resolve_env
from scrapers.alyc_sistemaF import MetroCorpScraper

_API_BASE = "https://be.bancocmf.com.ar/api/v1/execute"


async def main():
    fecha = sys.argv[1] if len(sys.argv) > 1 else "2026-02-02"

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    general = {**config["general"], "headless": False}
    alyc = next(a for a in config["alycs"] if a["nombre"] == "MetroCorp")

    async with MetroCorpScraper(alyc, general) as scraper:
        ok = await scraper.login()
        if not ok:
            print("Login fallido")
            return

        cuentas = alyc["opciones"].get("cuentas", [])
        fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_fmt = fecha_dt.strftime("%d/%m/%Y")
        iso_date = MetroCorpScraper._make_iso(fecha_fmt)

        for cuenta in cuentas:
            display_name = cuenta.get("display_name", "")
            cuenta_id    = cuenta.get("cuenta")
            id_env       = cuenta.get("id_environment")
            cta_nombre   = cuenta.get("nombre", "")

            if display_name:
                await scraper._switch_environment(display_name, 30_000)

            from scrapers.alyc_sistemaF import _URL_METROCORP
            from playwright.async_api import async_playwright
            page = scraper._page
            await page.goto(_URL_METROCORP, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(2000)

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
            print(f"\n{'='*60}")
            print(f"Cuenta: {cta_nombre} | Fecha: {fecha} | Total movimientos: {len(movements)}")
            print(f"{'='*60}")
            for m in movements:
                desc  = m.get("descripcionOperacion", "")
                nro   = m.get("numeroBoleto", "")
                ref   = m.get("referenciaMinuta", "")
                tipo  = scraper._classify(desc)
                print(f"  nro={nro:<12} ref={ref:<20} tipo_clasificado={str(tipo):<12}  desc={desc}")


if __name__ == "__main__":
    asyncio.run(main())

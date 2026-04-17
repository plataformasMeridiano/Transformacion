"""
test_dhalmore.py — Test del DhalmoreScraper.

Corre un download_tickets para una fecha de prueba y muestra los resultados.

Uso:
    python3 test_dhalmore.py [FECHA]   # default: ayer (2026-03-11)
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)

from scrapers.alyc_sistemaG import DhalmoreScraper

FECHA = sys.argv[1] if len(sys.argv) > 1 else "2026-03-11"

ALYC_CONFIG = {
    "nombre": "Dhalmore",
    "sistema": "sistemaG",
    "url_login": "https://clientes.dhalmorecap.com/",
    "usuario": "${DHALMORE_USUARIO}",
    "contrasena": "${DHALMORE_PASSWORD}",
    "opciones": {
        "headless": False,
        "tipo_operacion": ["Cauciones", "Pases"],
        "cuentas": [
            {"nombre": "MeridianoNorte", "customer_account_id": 56553},
            {"nombre": "Pamat",          "customer_account_id": 56555},
        ],
    },
}

GENERAL_CONFIG = {"headless": False, "download_dir": "downloads"}


async def main():
    dest = Path(f"downloads/test_dhalmore/{FECHA}")
    dest.mkdir(parents=True, exist_ok=True)

    async with DhalmoreScraper(ALYC_CONFIG, GENERAL_CONFIG) as scraper:
        ok = await scraper.login()
        if not ok:
            print("Login fallido")
            return

        pdfs = await scraper.download_tickets(FECHA, dest)
        print(f"\n=== {len(pdfs)} PDFs descargados ===")
        for p in pdfs:
            print(f"  {p.relative_to(dest.parent.parent)} ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    asyncio.run(main())

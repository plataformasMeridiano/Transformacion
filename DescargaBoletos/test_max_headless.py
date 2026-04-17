import asyncio
import json
import logging
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
from scrapers.alyc_sistemaE import MaxCapitalScraper

with open("config.json") as f:
    config = json.load(f)

# Simular main.py en producción: headless=True
general = {**config["general"], "headless": True}
alyc    = next(a for a in config["alycs"] if a["nombre"] == "MaxCapital")

async def main():
    dest = Path("downloads/MaxCapital_headless_test")
    dest.mkdir(parents=True, exist_ok=True)
    async with MaxCapitalScraper(alyc, general) as s:
        await s.login()
        files = await s.download_tickets("2026-02-25", dest)
    print(f"\nArchivos descargados ({len(files)}):")
    for f in files:
        print(f"  {f}  ({f.stat().st_size} bytes)")

asyncio.run(main())

"""
diag_adcap_codes.py — Lista todos los códigos de operación que aparecen
en el portal ADCAP para un rango de fechas.

Uso:
    python3 diag_adcap_codes.py 2026-04-01 2026-04-16
"""
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from batch_download import _resolve_env, business_days
from scrapers.alyc_sistemaB import AdcapScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)


async def main():
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    alyc_cfg = next(a for a in config["alycs"] if a["nombre"] == "ADCAP")
    general  = {**config["general"], "headless": True}

    inicio = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 4, 1)
    fin    = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()
    fechas = business_days(inicio, fin)

    logger.info("ADCAP — buscando todos los códigos en %d fechas (%s → %s)",
                len(fechas), inicio, fin)

    all_codes: Counter = Counter()

    async with AdcapScraper(alyc_cfg, general) as scraper:
        await scraper.login()

        for fecha in fechas:
            fecha_str = fecha if isinstance(fecha, str) else fecha.isoformat()
            from datetime import datetime
            fecha_fmt = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d/%m/%Y")

            timeout = alyc_cfg["opciones"].get("timeout_ms", 30000)
            cuentas = alyc_cfg["opciones"].get("cuentas", [{}])

            for cuenta in cuentas:
                label = cuenta.get("label", "")
                cta   = cuenta.get("nombre", "default")

                if label:
                    dashboard_url = (
                        alyc_cfg["url_login"].split("#")[0]
                        .replace("login.html", "desktop.html") + "#!/estado"
                    )
                    await scraper._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=timeout)
                    await asyncio.sleep(3)
                    await scraper._switch_cuenta(label, timeout)

                await scraper._navegar_boletos(timeout)
                await scraper._aplicar_filtro_fecha(fecha_str)

                rows = await scraper._leer_filas(fecha_fmt)
                for row in rows:
                    cells = row["cells"]
                    code = cells[4].strip().upper() if len(cells) > 4 else "?"
                    all_codes[code] += 1
                    logger.info("  %s [%s] code=%s  desc=%s",
                                fecha_str, cta, code,
                                cells[3][:40] if len(cells) > 3 else "")

    logger.info("\n=== RESUMEN CÓDIGOS ===")
    for code, count in all_codes.most_common():
        logger.info("  %-20s  %d veces", code, count)


if __name__ == "__main__":
    asyncio.run(main())

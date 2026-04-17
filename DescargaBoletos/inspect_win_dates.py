"""
inspect_win_dates.py — Inspecciona los inputs de fecha del portal WIN.
"""
import asyncio
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

_URL_PESOS_TIPO = "https://clientes.winsa.com.ar/Consultas/PesosPorTipoOperacion"


async def main():
    load_dotenv(Path(__file__).parent / ".env")
    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    win = next(a for a in config["alycs"] if a["nombre"] == "WIN")
    from scrapers.base_scraper import BaseScraper

    import os, re
    def resolve(v):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], v)

    usuario   = resolve(win["usuario"])
    contrasena = resolve(win["contrasena"])
    documento = resolve(win["documento"])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        page = await browser.new_page()

        await page.goto(win["url_login"], wait_until="load")
        await page.fill("input[name='Dni']", documento)
        await page.fill("#usuario", usuario)
        await page.fill("#passwd", contrasena)
        await page.click("#loginButton")
        await page.wait_for_url(lambda url: "/Login" not in url)
        logger.info("Login OK")

        # Cambiar a Pamat via dropdown visual
        await page.goto(_URL_PESOS_TIPO, wait_until="load")
        await page.wait_for_timeout(2000)
        await page.click(".select2-container .select2-selection")
        await page.wait_for_selector(".select2-results__option")
        await page.locator(".select2-results__option", has_text="50017").first.click()
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(3000)

        # Seleccionar Cauciones y consultar 25/03/26
        await page.select_option("#idInputTipoCombo1", value="03")  # Cauciones
        await page.click("button.boton-consulta")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(3000)

        # Imprimir encabezados y primeras filas de la tabla
        table_info = await page.evaluate("""() => {
            const headers = [...document.querySelectorAll('table thead th')]
                .map((th, i) => i + ': ' + th.innerText.trim());
            const rows = [...document.querySelectorAll('table tbody tr')]
                .slice(0, 5)
                .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()));
            return { headers, rows };
        }""")
        logger.info("=== Encabezados de la tabla ===")
        for h in table_info["headers"]:
            logger.info(h)
        logger.info("=== Primeras filas ===")
        for r in table_info["rows"]:
            logger.info(r)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

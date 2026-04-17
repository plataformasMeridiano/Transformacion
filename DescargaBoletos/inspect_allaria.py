"""
inspect_allaria.py — Inspecciona el portal Allaria usando el perfil guardado.
"""
import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

_URL_LOGIN  = "https://allaria.com.ar/Account/RedirectLogin"
_PROFILE_DIR = Path("browser_profiles/allaria")


async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,
            slow_mo=300,
            viewport={"width": 1280, "height": 800},
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        logger.info("Navegando a %s", _URL_LOGIN)
        await page.goto(_URL_LOGIN, wait_until="load")
        await page.wait_for_timeout(3000)

        info = await page.evaluate("""() => ({
            url: location.href,
            title: document.title,
            text: document.body.innerText.slice(0, 800),
        })""")
        logger.info("URL: %s", info["url"])
        logger.info("Título: %s", info["title"])
        logger.info("Texto:\n%s", info["text"])

        # Esperar cierre manual
        await ctx.wait_for_event("close", timeout=0)


if __name__ == "__main__":
    asyncio.run(main())

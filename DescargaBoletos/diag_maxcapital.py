"""
diag_maxcapital.py — Diagnóstico rápido: qué ve el browser en max.capital.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            slow_mo=50,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
        )
        page = await ctx.new_page()

        print("Navegando a home.max.capital (headless=False)...")
        await page.goto("https://home.max.capital/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)

        url = page.url
        title = await page.title()
        body_text = (await page.inner_text("body"))[:600]

        print(f"URL: {url}")
        print(f"Title: {title}")
        print(f"Body snippet:\n{body_text}")

        # Tomar screenshot
        shot = Path("/tmp/maxcap_diag.png")
        await page.screenshot(path=str(shot))
        print(f"Screenshot: {shot}")

        await browser.close()


asyncio.run(main())

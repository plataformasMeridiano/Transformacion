"""
Prueba directa de la API de movimientos de Puente con distintas combinaciones
de idCuenta, fechas y descripcion para aislar cuál filtro es el problema.
"""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
import os

with open("config.json") as f:
    cfg = json.load(f)

alyc = next(a for a in cfg["alycs"] if a["nombre"] == "Puente")

def resolve(v):
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], v)

URL_MOVS = "https://www.puentenet.com/cuentas/mi-cuenta/obtener-resultado-movimientos"


async def post_movimientos(ctx, **params) -> int:
    """POST al endpoint y retorna la cantidad de links de descarga en la respuesta."""
    body = urlencode(params)
    resp = await ctx.request.post(
        URL_MOVS,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30_000,
    )
    html = await resp.text()
    return html.count("descargar-pdf-movimiento")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        # Login
        await page.goto(alyc["url_login"], wait_until="domcontentloaded", timeout=60_000)
        await page.locator("#loginForm input[placeholder='Nro. documento']").fill(resolve(alyc["documento"]))
        await page.locator("#loginForm #input_username").fill(resolve(alyc["usuario"]))
        await page.locator("#loginForm #input_password").fill(resolve(alyc["contrasena"]))
        await page.locator("#loginForm").get_by_text("Ingresar", exact=True).click()
        await page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
        print(f"Login OK — {page.url}\n")

        tests = [
            # sin filtro, fechas largas (caso base — debería dar 3161)
            dict(idCuenta=18482, fechaDesde="03/12/2025", fechaHasta="02/03/2026", descripcion=""),
            # sin filtro, rango corto
            dict(idCuenta=18482, fechaDesde="25/02/2026", fechaHasta="28/02/2026", descripcion=""),
            # con filtro descripcion, rango corto
            dict(idCuenta=18482, fechaDesde="25/02/2026", fechaHasta="28/02/2026", descripcion="Caución Tomadora"),
            # con filtro descripcion, rango largo
            dict(idCuenta=18482, fechaDesde="03/12/2025", fechaHasta="02/03/2026", descripcion="Caución Tomadora"),
            # probar con otra descripción (Pase Tomador)
            dict(idCuenta=18482, fechaDesde="03/12/2025", fechaHasta="02/03/2026", descripcion="Pase Tomador"),
            # probar codificando la descripción con +
            dict(idCuenta=18482, fechaDesde="25/02/2026", fechaHasta="28/02/2026", descripcion="Caución+Tomadora"),
        ]

        for t in tests:
            n = await post_movimientos(context, **t)
            print(f"  {t}  →  {n} resultados")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

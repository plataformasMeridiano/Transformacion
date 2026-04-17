"""
Debug: verificar qué request envía el botón #traerMovimientos y qué retorna.
"""
import asyncio
import json
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
import os

with open("config.json") as f:
    cfg = json.load(f)

general = cfg["general"]
alyc = next(a for a in cfg["alycs"] if a["nombre"] == "Puente")

def resolve(v):
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], v)


_PROFILE_DIR = Path("browser_profiles/puente")


async def main():
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,
            executable_path="/usr/bin/google-chrome-stable",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Login
        url = alyc["url_login"]
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.locator("#loginForm input[placeholder='Nro. documento']").fill(resolve(alyc["documento"]))
        await page.locator("#loginForm #input_username").fill(resolve(alyc["usuario"]))
        await page.locator("#loginForm #input_password").fill(resolve(alyc["contrasena"]))
        await page.locator("#loginForm").get_by_text("Ingresar", exact=True).click()
        await page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
        print(f"Login OK — {page.url}")

        # Navegar a movimientos
        await page.goto("https://www.puentenet.com/cuentas/mi-cuenta/movimientos",
                        wait_until="load", timeout=30_000)
        await asyncio.sleep(1)

        # Capturar TODAS las requests
        all_reqs = []
        async def on_request(req):
            all_reqs.append({"url": req.url, "method": req.method, "body": req.post_data})

        page.on("request", on_request)

        # Paso 1: setear cuenta + descripción, luego fechas, luego click
        print("\n--- Setear cuenta + descripción + fechas y click ---")
        await page.select_option("#idCuenta", value="18482")
        await asyncio.sleep(2)
        await page.select_option("#descripcionFiltro", value="Caución Tomadora")
        await asyncio.sleep(2)

        # Verificar fechas actuales
        antes_desde = await page.input_value("#fechaDesde")
        antes_hasta = await page.input_value("#fechaHasta")
        print(f"  Fechas ANTES de fill: '{antes_desde}' → '{antes_hasta}'")

        # Intentar setear fechas via fill
        await page.fill("#fechaDesde", "25/02/2026")
        await page.fill("#fechaHasta", "28/02/2026")

        despues_desde = await page.input_value("#fechaDesde")
        despues_hasta = await page.input_value("#fechaHasta")
        print(f"  Fechas DESPUÉS de fill: '{despues_desde}' → '{despues_hasta}'")

        # Inspeccionar atributos de los inputs de fecha
        ng_info = await page.evaluate("""
            () => {
                const d = document.querySelector('#fechaDesde');
                const h = document.querySelector('#fechaHasta');
                return {
                    desde_value: d?.value,
                    hasta_value: h?.value,
                    desde_ngmodel: d?.getAttribute('ng-model'),
                    hasta_ngmodel: h?.getAttribute('ng-model'),
                    desde_type: d?.getAttribute('type'),
                };
            }
        """)
        print(f"  ng-model desde: {ng_info.get('desde_ngmodel')}")
        print(f"  ng-model hasta: {ng_info.get('hasta_ngmodel')}")
        print(f"  input type: {ng_info.get('desde_type')}")
        print(f"  DOM desde: {ng_info.get('desde_value')}")
        print(f"  DOM hasta: {ng_info.get('hasta_value')}")

        all_reqs.clear()
        await page.click("#traerMovimientos")
        await asyncio.sleep(5)

        print(f"\n  Requests capturadas: {len(all_reqs)}")
        for r in all_reqs:
            print(f"  [{r['method']}] {r['url']}")
            if r['body']:
                print(f"    body: {r['body'][:300]}")

        movs2 = await page.locator('a[href*="descargar-pdf-movimiento"]').count()
        print(f"  Movimientos visibles: {movs2}")

        # Primeras 3 filas de la tabla
        filas = await page.evaluate("""
            () => [...document.querySelectorAll('table tbody tr')]
                    .filter(tr => tr.querySelectorAll('td').length > 2)
                    .slice(0, 3)
                    .map(tr => ({
                        cells: [...tr.querySelectorAll('td')].map(td => td.innerText.trim()).slice(0,5),
                    }))
        """)
        for f in filas:
            print(f"  {f['cells']}")

        print("\nBrowser abierto 30s...")
        await asyncio.sleep(30)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
diag_puente_boleto_nro.py

Diagnóstico: busca de dónde sacar el número de boleto real en Puente.

Verifica:
  1. Header Content-Disposition del response de descarga
  2. HTML completo de las filas de la tabla (data-* attrs, celdas ocultas, etc.)
  3. innerText del link y sus atributos

Uso:
    python3 diag_puente_boleto_nro.py [fecha_dd/mm/yyyy]
    python3 diag_puente_boleto_nro.py 14/03/2026
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

with open("config.json") as f:
    cfg = json.load(f)

alyc = next(a for a in cfg["alycs"] if a["nombre"] == "Puente")

def resolve(v):
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], v)

_PROFILE_DIR = Path("browser_profiles/puente")
_BASE_URL = "https://www.puentenet.com"

fecha_test = sys.argv[1] if len(sys.argv) > 1 else "14/03/2026"


async def main():
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,
            executable_path="/usr/bin/google-chrome-stable",
            slow_mo=50,
            accept_downloads=True,
            viewport={"width": 1366, "height": 768},
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # ── Login ──────────────────────────────────────────────────────────
        print("[1] Login...")
        await page.goto(alyc["url_login"], wait_until="load", timeout=60_000)
        await page.locator("#loginForm input[placeholder='Nro. documento']").fill(resolve(alyc["documento"]))
        await page.locator("#loginForm #input_username").fill(resolve(alyc["usuario"]))
        await page.locator("#loginForm #input_password").fill(resolve(alyc["contrasena"]))
        await page.locator("#loginForm").get_by_text("Ingresar", exact=True).click()
        await page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
        print(f"    OK — {page.url}")

        # ── Movimientos ────────────────────────────────────────────────────
        print(f"\n[2] Movimientos — fecha: {fecha_test}")
        await page.goto(f"{_BASE_URL}/cuentas/mi-cuenta/movimientos", wait_until="load", timeout=30_000)
        await asyncio.sleep(1)

        # Tomar primera cuenta disponible
        cuentas = await page.evaluate("""
            () => [...document.querySelectorAll('#idCuenta option')]
                    .map(o => ({ value: o.value, label: o.text.trim() }))
                    .filter(o => o.value)
        """)
        if not cuentas:
            print("    ERROR: sin cuentas")
            await context.close()
            return

        cuenta = cuentas[0]
        print(f"    Cuenta: {cuenta['label']}")

        await page.select_option("#idCuenta", value=cuenta["value"])
        await asyncio.sleep(2)
        await page.select_option("#descripcionFiltro", value="Caución Tomadora")
        await asyncio.sleep(2)
        await page.fill("#fechaDesde", fecha_test)
        await page.fill("#fechaHasta", fecha_test)

        async with page.expect_response(
            lambda r: "obtener-resultado-movimientos" in r.url,
            timeout=30_000,
        ) as resp_info:
            await page.click("#traerMovimientos")
        await resp_info.value
        await asyncio.sleep(1)

        # ── Diagnóstico 1: HTML completo de filas con link de descarga ─────
        print("\n[3] HTML de filas con link de descarga:")
        filas_html = await page.evaluate("""
            () => {
                const links = document.querySelectorAll('a[href*="descargar-pdf-movimiento"]');
                return [...links].map(a => {
                    const tr = a.closest('tr');
                    return {
                        link_text:   a.innerText.trim(),
                        link_attrs:  [...a.attributes].map(at => `${at.name}="${at.value}"`).join(' '),
                        tr_html:     tr ? tr.outerHTML : null,
                        tr_datasets: tr ? JSON.stringify(tr.dataset) : null,
                        cells_text:  tr ? [...tr.querySelectorAll('td')].map(td => td.innerText.trim()) : [],
                    };
                });
            }
        """)

        if not filas_html:
            print("    No se encontraron movimientos para esta fecha/filtro.")
        else:
            for i, fila in enumerate(filas_html[:3]):  # Mostrar hasta 3
                print(f"\n  --- Fila {i+1} ---")
                print(f"  link_text : {fila['link_text']!r}")
                print(f"  link_attrs: {fila['link_attrs']}")
                print(f"  tr_dataset: {fila['tr_datasets']}")
                print(f"  cells_text: {fila['cells_text']}")
                if fila['tr_html']:
                    print(f"  tr_html   : {fila['tr_html'][:800]}")

        # ── Diagnóstico 2: Content-Disposition del primer PDF ──────────────
        if filas_html:
            first_href = await page.evaluate("""
                () => document.querySelector('a[href*="descargar-pdf-movimiento"]')?.getAttribute('href')
            """)
            if first_href:
                print(f"\n[4] Descargando primer PDF para ver headers...")
                print(f"    URL: {_BASE_URL}{first_href}")
                resp = await page.context.request.get(
                    f"{_BASE_URL}{first_href}",
                    timeout=30_000,
                )
                headers = dict(resp.headers)
                print(f"    Status: {resp.status}")
                print(f"    Headers relevantes:")
                for k, v in headers.items():
                    if any(x in k.lower() for x in ["content", "disposition", "filename"]):
                        print(f"      {k}: {v}")
                print(f"    Todos los headers: {list(headers.keys())}")

                body = await resp.body()
                print(f"    Body size: {len(body)} bytes")
                print(f"    Primeros bytes (ASCII): {body[:20]!r}")

        print("\n[OK] Diagnóstico completo. Browser abierto 20s...")
        await asyncio.sleep(20)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

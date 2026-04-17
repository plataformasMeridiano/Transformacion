"""
Test headless=True para Puente con delays anti-detección.
Verifica si el ban de Imperva ya fue levantado.

Resultado esperado:
  [LOGIN] OK  → ban levantado, login funciona
  [MOVS]  N   → cantidad de links de descarga encontrados
"""
import asyncio
import json
import os
import random
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

with open("config.json") as f:
    cfg = json.load(f)

alyc = next(a for a in cfg["alycs"] if a["nombre"] == "Puente")


def resolve(v: str) -> str:
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], v)


async def rand_delay(page, lo=300, hi=900):
    """Pausa aleatoria entre acciones para simular comportamiento humano."""
    await page.wait_for_timeout(random.randint(lo, hi))


async def human_fill(page, selector: str, value: str):
    """Hace click, pequeña pausa, y rellena campo carácter a carácter."""
    await page.click(selector)
    await rand_delay(page, 100, 300)
    await page.fill(selector, value)
    await rand_delay(page, 150, 400)


_PROFILE_DIR = Path("browser_profiles/puente")


async def main():
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,
            slow_mo=60,
            accept_downloads=True,
            viewport={"width": 1366, "height": 768},
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            executable_path="/usr/bin/google-chrome-stable",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        # Ocultar webdriver flag vía CDP
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        page = await context.new_page()

        # ── 1. Login ──────────────────────────────────────────────────────
        print("[1] Navegando a login...")
        await page.goto(alyc["url_login"], wait_until="domcontentloaded", timeout=60_000)
        await rand_delay(page, 1500, 2500)

        # Verificar si Imperva bloqueó
        content = await page.content()
        if "Request unsuccessful. Incapsula incident ID" in content or "Error 15 - not allowed" in content:
            print("[!] IMPERVA BLOCK detectado — ban sigue activo")
            print(f"    URL actual: {page.url}")
            print(f"    Contenido (800 chars): {content[:800]}")
            await context.close()
            return

        print(f"    URL: {page.url}")
        print(f"    Título: {await page.title()}")

        # Verificar que el form existe
        form_visible = await page.locator("#loginForm").is_visible()
        if not form_visible:
            print("[!] #loginForm no visible — posible block o cambio de página")
            print(f"    Body (500 chars): {content[:500]}")
            await context.close()
            return

        # Rellenar form con delays
        print("[1] Completando formulario...")
        await human_fill(page, "#loginForm input[placeholder='Nro. documento']", resolve(alyc["documento"]))
        await human_fill(page, "#loginForm #input_username", resolve(alyc["usuario"]))
        await human_fill(page, "#loginForm #input_password", resolve(alyc["contrasena"]))
        await rand_delay(page, 500, 1000)

        print("[1] Enviando...")
        await page.locator("#loginForm").get_by_text("Ingresar", exact=True).click()

        try:
            await page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
        except Exception:
            content = await page.content()
            if "Request unsuccessful. Incapsula incident ID" in content or "Error 15 - not allowed" in content:
                print("[!] IMPERVA BLOCK en post-login — ban sigue activo")
            else:
                print(f"[!] Login timeout — URL actual: {page.url}")
                print(f"    Body (300 chars): {content[:300]}")
            await context.close()
            return

        print(f"[LOGIN] OK — {page.url}")
        await rand_delay(page, 1000, 2000)

        # ── 2. Navegar a Movimientos ──────────────────────────────────────
        print("[2] Navegando a Movimientos...")
        await page.goto(
            "https://www.puentenet.com/cuentas/mi-cuenta/movimientos",
            wait_until="networkidle",
            timeout=30_000,
        )
        await rand_delay(page, 1000, 1500)

        content = await page.content()
        if "Request unsuccessful. Incapsula incident ID" in content or "Error 15 - not allowed" in content:
            print("[!] IMPERVA BLOCK en Movimientos")
            await context.close()
            return

        # Leer cuentas disponibles
        cuentas = await page.evaluate("""
            () => [...document.querySelectorAll('#idCuenta option')]
                    .map(o => ({ value: o.value, label: o.text.trim() }))
                    .filter(o => o.value)
        """)
        print(f"    Cuentas: {[c['label'] for c in cuentas]}")

        if not cuentas:
            print("[!] Sin cuentas en el select — revisar si la página cargó correctamente")
            await context.close()
            return

        # ── 3. Filtrar por Caución Tomadora, fecha reciente ───────────────
        cuenta = cuentas[0]
        fecha_test = "25/02/2026"
        print(f"\n[3] Probando cuenta={cuenta['label']} filtro='Caución Tomadora' fecha={fecha_test}")

        await page.select_option("#idCuenta", value=cuenta["value"])
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await rand_delay(page, 400, 800)

        await page.select_option("#descripcionFiltro", value="Caución Tomadora")
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await rand_delay(page, 300, 600)

        await page.fill("#fechaDesde", fecha_test)
        await page.fill("#fechaHasta", fecha_test)
        await rand_delay(page, 300, 600)

        await page.click("#traerMovimientos")
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await rand_delay(page, 800, 1200)

        movs = await page.evaluate("""
            () => [...document.querySelectorAll('a[href*="descargar-pdf-movimiento"]')]
                    .map(a => a.getAttribute('href'))
        """)
        print(f"    Movimientos encontrados: {len(movs)}")
        for href in movs:
            print(f"      {href}")

        # ── 4. Sin filtro de descripción, rango largo ──────────────────────
        print(f"\n[4] Sin filtro, rango 03/12/2025–28/02/2026")
        await page.select_option("#descripcionFiltro", value="")
        await rand_delay(page, 300, 500)
        await page.fill("#fechaDesde", "03/12/2025")
        await page.fill("#fechaHasta", "28/02/2026")
        await rand_delay(page, 300, 500)
        await page.click("#traerMovimientos")
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await rand_delay(page, 800, 1200)

        movs2 = await page.evaluate("""
            () => document.querySelectorAll('a[href*="descargar-pdf-movimiento"]').length
        """)
        print(f"    Movimientos encontrados: {movs2}")

        print("\n[OK] Test completo")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

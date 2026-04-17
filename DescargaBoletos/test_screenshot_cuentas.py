"""
Screenshot post-login de cada ALYC para verificar si existe selector de cuenta comitente.
Genera screenshots en downloads/screenshots_cuentas/{alyc}/
No descarga nada — solo captura pantallas.

Uso:
    python test_screenshot_cuentas.py [nombre_alyc]   # solo esa ALYC
    python test_screenshot_cuentas.py                  # todas
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

OUT_BASE = Path("downloads/screenshots_cuentas")
OUT_BASE.mkdir(parents=True, exist_ok=True)

SKIP = {"Puente"}  # ban Imperva activo


def resolve(v):
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], v)


def get_alyc(nombre):
    return next((a for a in cfg["alycs"] if a["nombre"] == nombre), None)


# ── WIN ──────────────────────────────────────────────────────────────────────
async def screenshot_win(out: Path):
    alyc = get_alyc("WIN")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        # login
        await page.goto(alyc["url_login"], wait_until="load", timeout=30_000)
        await page.fill("input[name='Dni']", resolve(alyc["documento"]))
        await page.fill("#usuario",          resolve(alyc["usuario"]))
        await page.fill("#passwd",           resolve(alyc["contrasena"]))
        await page.screenshot(path=str(out / "01_antes_login.png"), full_page=True)
        await page.click("#loginButton")
        await page.wait_for_url(lambda u: "/Login" not in u, timeout=30_000)
        await page.screenshot(path=str(out / "02_post_login.png"), full_page=True)

        # Navegar a la sección de consultas
        await page.goto("https://clientes.winsa.com.ar/Consultas/PesosPorTipoOperacion",
                        wait_until="load", timeout=30_000)
        await page.screenshot(path=str(out / "03_consultas.png"), full_page=True)

        # Buscar si hay algún selector de cuenta/comitente visible
        # Capturar el HTML completo del body para análisis
        inner = await page.inner_html("body")
        (out / "body.html").write_text(inner, encoding="utf-8")

        await browser.close()


# ── ADCAP ─────────────────────────────────────────────────────────────────────
async def screenshot_adcap(out: Path):
    alyc = get_alyc("ADCAP")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await page.goto(alyc["url_login"], wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("#input_0", timeout=15_000)
        await page.fill("#input_0", resolve(alyc["usuario"]))
        await page.fill("#input_1", resolve(alyc["contrasena"]))
        await page.screenshot(path=str(out / "01_antes_login.png"), full_page=True)
        await page.click("#btnIngresar")
        await page.wait_for_url(lambda u: "#!/login" not in u, timeout=30_000)
        await asyncio.sleep(4)
        await page.screenshot(path=str(out / "02_post_login.png"), full_page=True)

        # Intentar navegar a BOLETOS y ver si hay selector de cuenta
        await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('md-item-content[ng-click]'))
                    if (el.innerText.trim() === 'BOLETOS') { el.click(); break; }
            }
        """)
        await asyncio.sleep(4)
        await page.screenshot(path=str(out / "03_boletos.png"), full_page=True)

        # Ver menú lateral completo
        inner = await page.inner_html("body")
        (out / "body.html").write_text(inner, encoding="utf-8")

        await browser.close()


# ── BACS ──────────────────────────────────────────────────────────────────────
async def screenshot_bacs(out: Path):
    alyc = get_alyc("BACS")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await page.goto(alyc["url_login"], wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("#input_0", timeout=15_000)
        await page.fill("#input_0", resolve(alyc["usuario"]))
        await page.fill("#input_1", resolve(alyc["contrasena"]))
        await page.screenshot(path=str(out / "01_antes_login.png"), full_page=True)
        await page.click("#btnIngresar")
        await page.wait_for_url(lambda u: "#!/login" not in u, timeout=30_000)
        await asyncio.sleep(4)
        await page.screenshot(path=str(out / "02_post_login.png"), full_page=True)

        await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('md-item-content[ng-click]'))
                    if (el.innerText.trim() === 'BOLETOS') { el.click(); break; }
            }
        """)
        await asyncio.sleep(4)
        await page.screenshot(path=str(out / "03_boletos.png"), full_page=True)

        inner = await page.inner_html("body")
        (out / "body.html").write_text(inner, encoding="utf-8")

        await browser.close()


# ── MaxCapital ────────────────────────────────────────────────────────────────
async def screenshot_maxcapital(out: Path):
    alyc = get_alyc("MaxCapital")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await page.goto(alyc["url_login"], wait_until="networkidle", timeout=45_000)
        await page.wait_for_selector("#usernameLoginWeb", timeout=15_000)
        await page.fill("#usernameLoginWeb", resolve(alyc["usuario"]))
        await page.fill("#passwordLoginWeb", resolve(alyc["contrasena"]))
        await page.screenshot(path=str(out / "01_antes_login.png"), full_page=True)
        # El botón puede ser input[type='submit'] o button[type='submit'] según la versión de Keycloak
        submit = page.locator("input[type='submit'], button[type='submit']").first
        await submit.click(timeout=15_000)
        await asyncio.sleep(3)
        await page.screenshot(path=str(out / "02_post_submit.png"), full_page=True)

        # Pantalla "Select account" — capturar ANTES de elegir
        try:
            await page.wait_for_selector("input[type='radio']", timeout=10_000)
            await page.screenshot(path=str(out / "03_select_account.png"), full_page=True)
            inner = await page.inner_html("body")
            (out / "select_account.html").write_text(inner, encoding="utf-8")
        except Exception:
            await page.screenshot(path=str(out / "03_sin_selector.png"), full_page=True)

        await browser.close()


# ── ConoSur ───────────────────────────────────────────────────────────────────
async def screenshot_conosur(out: Path):
    alyc = get_alyc("ConoSur")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await page.goto(alyc["url_login"], wait_until="networkidle", timeout=30_000)
        await page.wait_for_selector("#usuario", timeout=15_000)
        await page.click("#usuario")
        await page.keyboard.type(resolve(alyc["usuario"]), delay=50)
        await page.click("#contraseña")
        await page.keyboard.type(resolve(alyc["contrasena"]), delay=50)
        await page.screenshot(path=str(out / "01_antes_login.png"), full_page=True)
        await page.click("button[type='submit']")
        await page.wait_for_url(lambda u: "/auth/signin" not in u, timeout=30_000)
        await asyncio.sleep(3)
        await page.screenshot(path=str(out / "02_post_login.png"), full_page=True)

        # Intentar navegar a Movimientos
        try:
            await page.goto(
                "https://virtualbroker-conosur.aunesa.com/movimientos",
                wait_until="networkidle", timeout=20_000
            )
            await asyncio.sleep(2)
            await page.screenshot(path=str(out / "03_movimientos.png"), full_page=True)
            # Guardar HTML desde movimientos (donde está el selector de cuenta)
            inner = await page.inner_html("body")
            (out / "movimientos.html").write_text(inner, encoding="utf-8")
            # Abrir el dropdown de cuenta en el header con hover
            try:
                trigger = page.locator("header .ant-dropdown-trigger").first
                await trigger.hover(timeout=5_000)
                await asyncio.sleep(1)
                await page.screenshot(path=str(out / "04_cuenta_dropdown.png"), full_page=True)
                # Hover sobre "Cambiar cuenta comitente" para abrir el submenu
                cambiar = page.locator("[class*='cambiar_cuenta'], li").get_by_text("Cambiar cuenta comitente").first
                await cambiar.hover(timeout=5_000)
                await asyncio.sleep(1)
                await page.screenshot(path=str(out / "05_cambiar_submenu.png"), full_page=True)
                inner3 = await page.inner_html("body")
                (out / "cambiar_cuenta.html").write_text(inner3, encoding="utf-8")
            except Exception as e:
                (out / "dropdown_error.txt").write_text(str(e))
        except Exception:
            pass

        await browser.close()


# ── MetroCorp ─────────────────────────────────────────────────────────────────
async def screenshot_metrocorp(out: Path):
    alyc = get_alyc("MetroCorp")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await page.goto(alyc["url_login"], wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(3000)
        await page.wait_for_selector("#document\\.number", timeout=30_000)
        await page.fill("#document\\.number", resolve(alyc["documento"]))
        await page.fill("#login\\.step1\\.username", resolve(alyc["usuario"]))
        await page.screenshot(path=str(out / "01_step1.png"), full_page=True)
        await page.click("button[type='submit']:has-text('Continuar')")

        await page.wait_for_selector("#login\\.step2\\.password", timeout=30_000)
        await page.screenshot(path=str(out / "02_step2.png"), full_page=True)
        await page.fill("#login\\.step2\\.password", resolve(alyc["contrasena"]))
        await page.click("button[type='submit']:has-text('Ingresar')")
        await page.wait_for_url(lambda u: "desktop" in u, timeout=30_000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(out / "03_post_login.png"), full_page=True)

        # Navegar a /metrocorp — pantalla principal de operaciones
        await page.goto("https://be.bancocmf.com.ar/metrocorp",
                        wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(out / "04_metrocorp.png"), full_page=True)

        inner = await page.inner_html("body")
        (out / "body.html").write_text(inner, encoding="utf-8")

        # Click en el botón environments-dropdown (empresa activa en el header)
        try:
            env_btn = page.locator("li.environments-dropdown button").first
            await env_btn.click(timeout=5_000)
            await page.wait_for_timeout(1000)
            await page.screenshot(path=str(out / "05_environments_dropdown.png"), full_page=True)
            inner2 = await page.inner_html("body")
            (out / "environments.html").write_text(inner2, encoding="utf-8")
        except Exception as e:
            (out / "env_error.txt").write_text(str(e))

        # Click en el react-select "Cuenta comitente" para ver opciones disponibles
        try:
            acct_select = page.locator("#account")
            await acct_select.click(timeout=5_000)
            await page.wait_for_timeout(1000)
            await page.screenshot(path=str(out / "06_cuenta_options.png"), full_page=True)
            inner3 = await page.inner_html("body")
            (out / "cuenta_options.html").write_text(inner3, encoding="utf-8")
        except Exception as e:
            (out / "cuenta_error.txt").write_text(str(e))

        await browser.close()


# ── Dispatcher ────────────────────────────────────────────────────────────────
TASKS = {
    "WIN":        (screenshot_win,        "WIN"),
    "ADCAP":      (screenshot_adcap,      "ADCAP"),
    "BACS":       (screenshot_bacs,       "BACS"),
    "MaxCapital": (screenshot_maxcapital, "MaxCapital"),
    "ConoSur":    (screenshot_conosur,    "ConoSur"),
    "MetroCorp":  (screenshot_metrocorp,  "MetroCorp"),
}


async def main():
    filtro = sys.argv[1].strip() if len(sys.argv) > 1 else None

    for nombre, (fn, _) in TASKS.items():
        if filtro and nombre != filtro:
            continue
        if nombre in SKIP:
            print(f"[SKIP] {nombre} — ban Imperva")
            continue

        out = OUT_BASE / nombre
        out.mkdir(parents=True, exist_ok=True)
        print(f"[>>>] {nombre} ...", end="", flush=True)
        try:
            await fn(out)
            shots = sorted(out.glob("*.png"))
            print(f"  OK — {len(shots)} screenshots en {out}")
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())

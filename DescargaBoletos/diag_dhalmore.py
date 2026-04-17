"""
diag_dhalmore.py — Explora el portal Dhalmore Capital.

1. Abre el browser en https://clientes.dhalmorecap.com/
2. Intenta login (email + password)
3. Pausa para MFA manual
4. Explora la sección de boletos/movimientos
5. Inspecciona la red (XHR/fetch) para identificar endpoints de descarga

Uso:
    python3 diag_dhalmore.py
"""
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(".env"))
import os

USUARIO  = os.environ.get("DHALMORE_USUARIO",  "djoy@meridianonorte.com")
PASSWORD = os.environ.get("DHALMORE_PASSWORD", "")

URL_LOGIN = "https://clientes.dhalmorecap.com/"
OUT_DIR   = Path("downloads/diag_dhalmore")
OUT_DIR.mkdir(parents=True, exist_ok=True)

from playwright.async_api import async_playwright


async def main():
    requests_log  = []
    responses_log = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        page = await context.new_page()

        # Interceptar todas las requests/responses para analizar la API
        async def on_request(req):
            if req.resource_type in ("xhr", "fetch"):
                requests_log.append({
                    "method": req.method,
                    "url":    req.url,
                    "post":   req.post_data,
                })

        async def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                ct = resp.headers.get("content-type", "")
                try:
                    body = await resp.body()
                    decoded = body[:500].decode("utf-8", errors="replace")
                except Exception:
                    decoded = "(error leyendo body)"
                responses_log.append({
                    "status": resp.status,
                    "url":    resp.url,
                    "ct":     ct,
                    "body":   decoded,
                })

        page.on("request",  on_request)
        page.on("response", on_response)

        # ── 1. Login ────────────────────────────────────────────────────────
        print(f"\n[1] Navegando a {URL_LOGIN}")
        await page.goto(URL_LOGIN, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUT_DIR / "01_inicio.png"))
        print(f"  URL: {page.url}")
        print(f"  Título: {await page.title()}")

        # Mostrar campos de login disponibles
        inputs = await page.query_selector_all("input")
        print(f"  Inputs encontrados: {len(inputs)}")
        for inp in inputs:
            t  = await inp.get_attribute("type") or "?"
            n  = await inp.get_attribute("name") or ""
            p  = await inp.get_attribute("placeholder") or ""
            id_ = await inp.get_attribute("id") or ""
            print(f"    type={t} name={n!r} id={id_!r} placeholder={p!r}")

        # Intentar llenar email y password
        print(f"\n[2] Intentando login con {USUARIO}")
        # Buscar campo email
        for sel in ["input[type='email']", "input[name='email']", "input[name='username']",
                    "input[placeholder*='mail' i]", "input[placeholder*='user' i]"]:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(USUARIO)
                print(f"  Email llenado en: {sel}")
                break

        # Buscar campo password
        for sel in ["input[type='password']"]:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(PASSWORD)
                print(f"  Password llenado")
                break

        await page.screenshot(path=str(OUT_DIR / "02_login_lleno.png"))

        # Click submit
        for sel in ["button[type='submit']", "input[type='submit']",
                    "button:has-text('Ingresar')", "button:has-text('Login')",
                    "button:has-text('Iniciar')", "button:has-text('Entrar')"]:
            el = page.locator(sel).first
            if await el.count() > 0:
                print(f"  Haciendo click en: {sel}")
                await el.click()
                break

        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT_DIR / "03_post_submit.png"))
        print(f"  URL post-submit: {page.url}")

        # ── 3. MFA ──────────────────────────────────────────────────────────
        print(f"""
========================================================
  PAUSA — MFA REQUERIDO

  El browser está abierto. Por favor:
  1. Revisá si llegó un código por email/SMS
  2. Ingresalo en el browser
  3. Completá el login manualmente

  Tenés 120 segundos.
========================================================
        """)
        await page.screenshot(path=str(OUT_DIR / "04_mfa.png"))
        await page.wait_for_timeout(120_000)

        # ── 4. Post-login ────────────────────────────────────────────────────
        await page.screenshot(path=str(OUT_DIR / "05_post_login.png"))
        print(f"\n[4] Post-login:")
        print(f"  URL: {page.url}")
        print(f"  Título: {await page.title()}")

        # Guardar HTML completo
        html = await page.content()
        (OUT_DIR / "post_login.html").write_text(html, encoding="utf-8")
        print(f"  HTML guardado en post_login.html")

        # Mostrar links/menús visibles
        links = await page.query_selector_all("a, button, [role='menuitem'], nav *")
        print(f"\n  Links/botones principales ({len(links)} total):")
        seen = set()
        for el in links[:100]:
            txt = (await el.inner_text()).strip()
            if txt and len(txt) < 50 and txt not in seen:
                seen.add(txt)
                print(f"    {txt!r}")

        # ── 5. Requests capturadas ────────────────────────────────────────────
        print(f"\n[5] Requests XHR/Fetch capturadas ({len(requests_log)}):")
        for r in requests_log[-30:]:
            print(f"  {r['method']} {r['url'][:100]}")
            if r.get("post"):
                print(f"    body: {r['post'][:100]!r}")

        # Guardar log
        (OUT_DIR / "requests.json").write_text(
            json.dumps({"requests": requests_log, "responses": responses_log},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n  Log completo guardado en requests.json")

        # ── 6. Exploración manual ────────────────────────────────────────────
        print(f"""
========================================================
  EXPLORACIÓN MANUAL (60s)

  Navegá a la sección de Boletos/Cauciones/Pases.
  Abrí DevTools → Network para ver los endpoints.
========================================================
        """)
        await page.wait_for_timeout(60_000)

        await context.close()
        await browser.close()

    print(f"\n[FIN] Screenshots y logs en {OUT_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())

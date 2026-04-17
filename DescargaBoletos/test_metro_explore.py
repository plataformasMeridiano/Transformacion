"""
Exploración del portal Metrocorp (be.bancocmf.com.ar).
Objetivo:
  1. Cargar la página y ver el formulario de login
  2. Hacer login y explorar secciones de boletos/comprobantes
  3. Monitorear las llamadas API que se hacen al navegar
  4. Intentar descargar un PDF de boleto

Uso:
    python test_metro_explore.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

URL_BASE  = "https://be.bancocmf.com.ar"
URL_LOGIN = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT_DIR = Path("downloads/metro_explore")
OUT_DIR.mkdir(parents=True, exist_ok=True)

requests_log: list[dict] = []
responses_log: list[dict] = []


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=200)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # ── Interceptores de red ────────────────────────────────────────────
        async def on_request(req):
            if req.resource_type in ("xhr", "fetch"):
                entry = {"url": req.url, "method": req.method}
                try:
                    entry["post_data"] = req.post_data
                except Exception:
                    pass
                requests_log.append(entry)

        async def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                entry = {"url": resp.url, "status": resp.status,
                         "ct": resp.headers.get("content-type", "")}
                try:
                    body = await resp.body()
                    if b"%PDF" in body[:10]:
                        entry["body_type"] = "PDF"
                        entry["body_len"] = len(body)
                    elif "json" in entry["ct"]:
                        entry["body"] = (await resp.json())
                    else:
                        entry["body_preview"] = body[:300].decode("utf-8", errors="replace")
                except Exception as e:
                    entry["body_err"] = str(e)
                responses_log.append(entry)

        page.on("request",  on_request)
        page.on("response", on_response)

        # ── 1. Cargar página principal ──────────────────────────────────────
        print(f"\n[1] Navegando a {URL_LOGIN}")
        await page.goto(URL_LOGIN, wait_until="networkidle", timeout=60_000)
        await page.screenshot(path=str(OUT_DIR / "01_landing.png"))
        print(f"    URL: {page.url}")

        # Volcar todos los inputs visibles
        inputs = await page.locator("input").all()
        print(f"    Inputs encontrados: {len(inputs)}")
        for i, inp in enumerate(inputs):
            try:
                attrs = await inp.evaluate(
                    "el => ({ id: el.id, name: el.name, type: el.type, placeholder: el.placeholder, class: el.className })"
                )
                print(f"      [{i}] {attrs}")
            except Exception:
                pass

        # Volcar todos los labels
        labels = await page.locator("label").all()
        print(f"    Labels: {len(labels)}")
        for lbl in labels:
            try:
                txt = await lbl.text_content()
                print(f"      label: {txt!r}")
            except Exception:
                pass

        # ── 2. Completar login ──────────────────────────────────────────────
        print("\n[2] Intentando login...")

        # Intentar con los selectores más comunes
        # React SPAs suelen usar name="" o placeholder="" o clases específicas
        try:
            # Primero ver si hay campos de DNI/usuario/contraseña
            all_inputs = await page.evaluate("""
                () => Array.from(document.querySelectorAll('input')).map(el => ({
                    id: el.id,
                    name: el.name,
                    type: el.type,
                    placeholder: el.placeholder,
                    className: el.className.substring(0, 80),
                    'aria-label': el.getAttribute('aria-label'),
                    value: el.value
                }))
            """)
            print("    Inputs en DOM:")
            for inp in all_inputs:
                print(f"      {inp}")
        except Exception as e:
            print(f"    Error evaluando DOM: {e}")

        # Intentar login con campos comunes
        filled = False

        # Patrón 1: campos separados DNI + usuario + contraseña
        try:
            # Buscar campo de tipo documento/DNI
            dni_sel = "input[name='dni'], input[placeholder*='DNI'], input[placeholder*='dni'], input[id*='dni'], input[id*='DNI'], input[id*='documento']"
            if await page.locator(dni_sel).count() > 0:
                await page.fill(dni_sel, DNI)
                print(f"    DNI llenado con selector: {dni_sel}")
                filled = True
        except Exception as e:
            print(f"    DNI selector failed: {e}")

        try:
            usr_sel = "input[name='username'], input[name='usuario'], input[id*='user'], input[id*='usuario'], input[placeholder*='sario'], input[placeholder*='sername']"
            if await page.locator(usr_sel).count() > 0:
                await page.fill(usr_sel, USUARIO)
                print(f"    Usuario llenado")
        except Exception as e:
            print(f"    Usuario selector failed: {e}")

        try:
            pwd_sel = "input[type='password']"
            if await page.locator(pwd_sel).count() > 0:
                await page.fill(pwd_sel, PASSWORD)
                print(f"    Contraseña llenada")
        except Exception as e:
            print(f"    Password selector failed: {e}")

        await page.screenshot(path=str(OUT_DIR / "02_form_filled.png"))

        # Click en submit
        try:
            btn_sel = "button[type='submit'], input[type='submit'], button:has-text('Ingresar'), button:has-text('Login'), button:has-text('Entrar'), button:has-text('Iniciar')"
            btn = page.locator(btn_sel).first
            btn_text = await btn.text_content()
            print(f"    Botón encontrado: {btn_text!r}")
            await btn.click()
        except Exception as e:
            print(f"    Submit button fallido: {e}")
            # Intentar con Enter
            await page.keyboard.press("Enter")

        # Esperar navegación post-login
        try:
            await page.wait_for_url(lambda u: u != URL_LOGIN and "login" not in u.lower(), timeout=15_000)
            print(f"    Post-login URL: {page.url}")
        except Exception as e:
            print(f"    Timeout esperando URL: {e}")
            print(f"    URL actual: {page.url}")

        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT_DIR / "03_post_login.png"))

        # ── 3. Explorar secciones disponibles ──────────────────────────────
        print("\n[3] Explorando secciones disponibles...")
        nav_links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('nav a, [role="navigation"] a, aside a, .menu a, .sidebar a'))
                .map(a => ({ text: a.textContent.trim().substring(0, 60), href: a.href }))
                .filter(a => a.text)
        """)
        print(f"    Links de navegación: {len(nav_links)}")
        for lnk in nav_links:
            print(f"      {lnk['text']!r} → {lnk['href']}")

        # Buscar sección de boletos/operaciones
        boleto_words = ["boleto", "operaci", "comprobante", "ticket", "download", "estado", "historial"]
        for word in boleto_words:
            matches = await page.locator(f"text=/{word}/i").all()
            if matches:
                texts = []
                for m in matches[:5]:
                    try:
                        texts.append(await m.text_content())
                    except Exception:
                        pass
                print(f"    Texto '{word}': {[t.strip()[:60] for t in texts]}")

        # ── 4. Examinar llamadas API recientes ──────────────────────────────
        print(f"\n[4] Llamadas API capturadas: {len(responses_log)}")
        for r in responses_log[:30]:
            body_info = ""
            if "body" in r:
                body_info = f" body_keys={list(r['body'].keys())[:8]}" if isinstance(r['body'], dict) else f" body={str(r['body'])[:100]}"
            elif "body_preview" in r:
                body_info = f" preview={r['body_preview'][:80]!r}"
            elif "body_type" in r:
                body_info = f" PDF={r['body_len']}b"
            print(f"    [{r['status']}] {r['method'] if 'method' in r else ''} {r['url'][:100]}{body_info}")

        # ── 5. Intentar navegar a sección de boletos ────────────────────────
        print("\n[5] Intentando navegar a boletos/operaciones...")

        # Primero ver todo el HTML de la página actual
        page_title = await page.title()
        print(f"    Título de página: {page_title!r}")

        all_buttons = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button, a[href]')).map(el => ({
                tag: el.tagName,
                text: el.textContent.trim().substring(0, 60),
                href: el.getAttribute('href'),
                class: el.className.substring(0, 50)
            })).filter(el => el.text)
        """)
        print(f"    Todos los botones/links ({len(all_buttons)}):")
        for btn in all_buttons[:40]:
            print(f"      <{btn['tag']}> {btn['text']!r} href={btn['href']!r}")

        # Tomar screenshot final con la página actual
        await page.screenshot(path=str(OUT_DIR / "04_explore.png"), full_page=True)

        # ── 6. Guardar log completo ─────────────────────────────────────────
        with open(OUT_DIR / "api_log.json", "w") as f:
            json.dump({
                "requests": requests_log,
                "responses": responses_log,
            }, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n[6] Log guardado en {OUT_DIR}/api_log.json")

        print("\n[Pausa de 10s para revisar el browser antes de cerrar...]")
        await page.wait_for_timeout(10_000)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

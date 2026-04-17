"""
Login completo en Metrocorp + exploración post-login.
Flujo detectado:
  Step 1: DNI (#document.number) + Usuario (#login.step1.username) → "Continuar"
  Step 2: Contraseña → "Ingresar" (o similar)

Luego explora secciones de boletos y captura las APIs.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL      = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

api_responses: list[dict] = []


async def log_responses(page):
    async def on_response(resp):
        if resp.request.resource_type in ("xhr", "fetch"):
            entry = {"url": resp.url, "status": resp.status,
                     "method": resp.request.method,
                     "ct": resp.headers.get("content-type", "")}
            try:
                body = await resp.body()
                if b"%PDF" in body[:10]:
                    entry["body_type"] = "PDF"
                    entry["body_len"] = len(body)
                elif "json" in entry["ct"]:
                    entry["body"] = json.loads(body)
                else:
                    entry["body_preview"] = body[:400].decode("utf-8", errors="replace")
            except Exception as e:
                entry["err"] = str(e)
            api_responses.append(entry)
            # Print en tiempo real
            b = entry.get("body", {})
            info = f"keys={list(b.keys())[:4]}" if isinstance(b, dict) else entry.get("body_preview", "")[:80]
            print(f"  API [{entry['status']}] {entry['method']} {entry['url'][:90]}")
            if info:
                print(f"       {info}")

    page.on("response", on_response)


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=150)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await log_responses(page)

        # ── Step 1: Cargar y llenar DNI + Usuario ───────────────────────────
        print("[1] Cargando página...")
        await page.goto(URL, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(2000)

        print("[2] Llenando DNI y Usuario...")
        # El id tiene puntos — en Playwright usar el escape de CSS: #document\\.number
        await page.fill("#document\\.number", DNI)
        await page.fill("#login\\.step1\\.username", USUARIO)

        await page.screenshot(path=str(OUT / "30_step1_filled.png"))
        print("    Fields llenados. Haciendo click en Continuar...")

        await page.click("button[type='submit']:has-text('Continuar')")
        await page.wait_for_timeout(3000)

        await page.screenshot(path=str(OUT / "31_after_continuar.png"))
        print(f"    URL post-Continuar: {page.url}")

        # ── Detectar Step 2 ────────────────────────────────────────────────
        inputs2 = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).map(el => ({
                id: el.id, name: el.name, type: el.type,
                placeholder: el.placeholder,
                visible: el.offsetParent !== null
            }))
        """)
        print(f"\n[3] Inputs en Step 2: {inputs2}")

        buttons2 = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button')).map(el => ({
                type: el.type,
                text: el.textContent.trim().substring(0, 60),
                visible: el.offsetParent !== null
            })).filter(b => b.visible)
        """)
        print(f"    Buttons: {buttons2}")

        # Llenar contraseña (step 2)
        pwd_inputs = [i for i in inputs2 if i['type'] == 'password' and i['visible']]
        print(f"    Password inputs visibles: {pwd_inputs}")

        if pwd_inputs:
            pwd_id = pwd_inputs[0]['id']
            if pwd_id:
                await page.fill(f"#{pwd_id.replace('.', '\\.')}", PASSWORD)
            else:
                await page.fill("input[type='password']:visible", PASSWORD)
            print(f"    Contraseña llenada en {pwd_id!r}")
            await page.screenshot(path=str(OUT / "32_step2_filled.png"))

            # Submit step 2
            submit_btn = page.locator("button[type='submit']").first
            btn_text = await submit_btn.text_content()
            print(f"    Clickeando botón: {btn_text!r}")
            await submit_btn.click()
            await page.wait_for_timeout(5000)
        else:
            # Quizás solo hay 1 step, buscar contraseña directa
            print("    No hay paso 2 con password visible — probando fill directo")
            try:
                await page.fill("input[type='password']", PASSWORD)
                await page.click("button[type='submit']")
                await page.wait_for_timeout(5000)
            except Exception as e:
                print(f"    Error: {e}")

        await page.screenshot(path=str(OUT / "33_post_login.png"))
        print(f"\n[4] URL post-login: {page.url}")

        # ── Explorar post-login ────────────────────────────────────────────
        await page.wait_for_timeout(3000)

        title = await page.title()
        print(f"    Título: {title!r}")

        # Ver todos los links/botones de navegación
        nav = await page.evaluate("""
            () => Array.from(document.querySelectorAll(
                'nav a, nav button, [role="navigation"] a, aside a, aside button, ' +
                '.menu a, .sidebar a, [class*="menu"] a, [class*="nav"] a'
            )).map(el => ({
                tag: el.tagName,
                text: el.textContent.trim().substring(0, 80),
                href: el.getAttribute('href') || ''
            })).filter(el => el.text)
        """)
        print(f"\n    Navegación ({len(nav)}):")
        for n in nav:
            print(f"      <{n['tag']}> {n['text']!r} → {n['href']!r}")

        # Buscar texto de "boleto", "comprobante", "operaciones", etc.
        for word in ["boleto", "comprobante", "operaci", "historial", "estado"]:
            count = await page.locator(f"text=/{word}/i").count()
            if count > 0:
                texts = []
                for i in range(min(3, count)):
                    try:
                        t = await page.locator(f"text=/{word}/i").nth(i).text_content()
                        texts.append(t.strip()[:60])
                    except Exception:
                        pass
                print(f"    '{word}' aparece {count}x: {texts}")

        # Dump completo del HTML post-login
        html = await page.evaluate("() => document.body.innerHTML")
        with open(OUT / "post_login_body.txt", "w") as f:
            f.write(html)
        print(f"\n    HTML post-login guardado ({len(html)} chars)")

        # ── Intentar navegar a boletos ─────────────────────────────────────
        print("\n[5] Intentando navegar a sección de boletos...")

        # Ver todas las URLs internas disponibles
        all_links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href], [ng-href], [routerlink]')).map(el => ({
                text: el.textContent.trim().substring(0, 60),
                href: el.href || el.getAttribute('ng-href') || el.getAttribute('routerlink') || ''
            })).filter(el => el.href && !el.href.includes('javascript'))
        """)
        print(f"    Links ({len(all_links)}):")
        for lnk in all_links[:30]:
            print(f"      {lnk['text']!r} → {lnk['href']!r}")

        # Guardar todas las API responses
        with open(OUT / "api_log3.json", "w") as f:
            json.dump(api_responses, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n[6] Log guardado en {OUT}/api_log3.json")

        await page.screenshot(path=str(OUT / "34_final.png"), full_page=True)

        # Pausa para inspección manual
        print("\n[Esperando 15s antes de cerrar...]")
        await page.wait_for_timeout(15_000)

        await context.close()
        await browser.close()

    # ── Resumen de APIs ────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("RESUMEN DE TODAS LAS APIs LLAMADAS:")
    print("="*60)
    for r in api_responses:
        url = r['url']
        if 'api' in url.lower() or 'execute' in url.lower():
            b = r.get("body", {})
            if isinstance(b, dict):
                data_keys = list(b.get("data", {}).keys())[:5] if isinstance(b.get("data"), dict) else "..."
                info = f"data_keys={data_keys}" if data_keys != "..." else f"keys={list(b.keys())[:5]}"
            else:
                info = r.get("body_preview", "")[:80]
            print(f"  [{r['status']}] {r['method']} {url}")
            print(f"    {info}")


if __name__ == "__main__":
    asyncio.run(main())

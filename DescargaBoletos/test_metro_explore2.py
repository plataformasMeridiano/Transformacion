"""
Exploración detallada del formulario de login de Metrocorp.
Espera a que el React app renderice y vuelca el HTML completo.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL  = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

api_responses: list[dict] = []


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        async def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                entry = {"url": resp.url, "status": resp.status,
                         "method": resp.request.method,
                         "ct": resp.headers.get("content-type", "")}
                try:
                    if "json" in entry["ct"]:
                        entry["body"] = await resp.json()
                    else:
                        body = await resp.body()
                        entry["body_preview"] = body[:400].decode("utf-8", errors="replace")
                except Exception as e:
                    entry["err"] = str(e)
                api_responses.append(entry)

        page.on("response", on_response)

        # ── Cargar y esperar render ──────────────────────────────────────────
        print("[1] Cargando página...")
        await page.goto(URL, wait_until="networkidle", timeout=60_000)
        # Extra wait para React
        await page.wait_for_timeout(3000)

        # Volcar HTML completo del body
        html = await page.evaluate("() => document.body.innerHTML")
        with open(OUT / "body_html.txt", "w") as f:
            f.write(html)
        print(f"    HTML guardado ({len(html)} chars)")

        # Ver todos los elementos de input
        all_elements = await page.evaluate("""
            () => {
                const selectors = ['input', 'textarea', '[contenteditable]', '[role="textbox"]',
                                   '[class*="input"]', '[class*="Input"]', '[class*="field"]',
                                   'form', 'button'];
                const result = {};
                for (const sel of selectors) {
                    const els = Array.from(document.querySelectorAll(sel));
                    result[sel] = els.map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        name: el.getAttribute('name'),
                        type: el.type || el.getAttribute('type'),
                        placeholder: el.placeholder || el.getAttribute('placeholder'),
                        class: (el.className || '').substring(0, 80),
                        'aria-label': el.getAttribute('aria-label'),
                        'data-testid': el.getAttribute('data-testid'),
                        visible: el.offsetParent !== null,
                        value: el.value ? el.value.substring(0, 20) : ''
                    }));
                }
                return result;
            }
        """)
        print("\n[2] Elementos en DOM:")
        for sel, els in all_elements.items():
            if els:
                print(f"  {sel}: {len(els)} elementos")
                for el in els[:5]:
                    print(f"    {el}")

        # Screenshot
        await page.screenshot(path=str(OUT / "10_initial.png"), full_page=True)

        # ── Intentar llenar el formulario ──────────────────────────────────
        print("\n[3] Llenando formulario...")

        # Esperar a que algún input sea visible
        try:
            await page.wait_for_selector("input:visible, [role='textbox']:visible", timeout=5000)
            print("    Input visible encontrado!")
        except Exception:
            print("    No hay inputs visibles. Probando con first visible input anyway...")

        # Dump de todos los inputs con información de visibilidad
        inputs_detail = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).map(el => {
                const rect = el.getBoundingClientRect();
                return {
                    id: el.id,
                    name: el.name,
                    type: el.type,
                    placeholder: el.placeholder,
                    class: el.className.substring(0, 80),
                    'aria-label': el.getAttribute('aria-label'),
                    visible: el.offsetParent !== null,
                    rect: { top: rect.top, left: rect.left, w: rect.width, h: rect.height },
                    tabIndex: el.tabIndex,
                    disabled: el.disabled,
                    readOnly: el.readOnly
                };
            })
        """)
        print(f"    Inputs totales: {len(inputs_detail)}")
        for inp in inputs_detail:
            print(f"      {inp}")

        # Intentar click en el primer input/form field
        # A veces los React inputs tienen class que incluye 'input'
        for attempt_sel in [
            "input:not([type='hidden'])",
            "[placeholder]",
            "form input",
            ".input input",
            "input[type='text']",
            "input[type='number']",
            "input[type='tel']",
        ]:
            count = await page.locator(attempt_sel).count()
            if count > 0:
                print(f"    Encontrado con selector {attempt_sel!r}: {count}")

        # Ver toda la estructura de forms
        forms = await page.evaluate("""
            () => Array.from(document.querySelectorAll('form')).map(f => ({
                id: f.id,
                class: f.className.substring(0, 80),
                innerHTML: f.innerHTML.substring(0, 500)
            }))
        """)
        print(f"\n    Forms: {len(forms)}")
        for frm in forms:
            print(f"      {frm['id']!r} class={frm['class']!r}")
            print(f"      HTML: {frm['innerHTML'][:300]}")

        # ── Intentar login con keyboard ────────────────────────────────────
        print("\n[4] Intentando login via keyboard Tab+typing...")
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(200)
        focused = await page.evaluate("() => ({ tag: document.activeElement.tagName, id: document.activeElement.id, class: document.activeElement.className.substring(0,60) })")
        print(f"    Focused after Tab: {focused}")

        # Try clicking on visible area where input should be
        # and then type
        await page.keyboard.type(DNI, delay=50)
        await page.wait_for_timeout(500)

        await page.screenshot(path=str(OUT / "11_after_dni.png"), full_page=True)

        # Check if value appeared somewhere
        current_inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).map(el => ({
                id: el.id, name: el.name, value: el.value, type: el.type
            }))
        """)
        print(f"    Inputs after typing DNI: {current_inputs}")

        # ── Ver las APIs llamadas hasta ahora ──────────────────────────────
        print("\n[5] Llamadas API registradas:")
        for r in api_responses:
            body = r.get("body", {})
            preview = r.get("body_preview", "")
            if isinstance(body, dict):
                keys = list(body.keys())[:5]
                data_preview = str(body.get("data", ""))[:150] if "data" in body else ""
                info = f"keys={keys} data={data_preview}"
            else:
                info = preview[:100]
            print(f"  [{r['status']}] {r['method']} {r['url']}")
            print(f"    {info}")

        # ── Guardar HTML final ─────────────────────────────────────────────
        html2 = await page.evaluate("() => document.body.innerHTML")
        with open(OUT / "body_html2.txt", "w") as f:
            f.write(html2)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

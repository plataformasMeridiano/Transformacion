"""
Exploración Metrocorp con headless=False — dump completo de HTML.
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
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

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
        print("[1] Cargando página (headless=False)...")
        await page.goto(URL, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3000)

        html = await page.evaluate("() => document.body.innerHTML")
        print(f"    HTML body length: {len(html)} chars")
        with open(OUT / "body_headful.txt", "w") as f:
            f.write(html)
        print(f"    HTML guardado en body_headful.txt")

        # Inputs y elementos clave
        elements = await page.evaluate("""
            () => {
                const all = Array.from(document.querySelectorAll(
                    'input, textarea, [contenteditable], [role="textbox"], button'
                ));
                return all.map(el => {
                    const rect = el.getBoundingClientRect();
                    return {
                        tag: el.tagName,
                        id: el.id,
                        name: el.getAttribute('name'),
                        type: el.type || '',
                        placeholder: el.placeholder || el.getAttribute('placeholder') || '',
                        class: (el.className || '').substring(0, 100),
                        'aria-label': el.getAttribute('aria-label') || '',
                        'data-testid': el.getAttribute('data-testid') || '',
                        text: el.textContent ? el.textContent.trim().substring(0, 60) : '',
                        visible: el.offsetParent !== null,
                        rect: { w: Math.round(rect.width), h: Math.round(rect.height) }
                    };
                });
            }
        """)
        print(f"\n[2] Todos los elementos ({len(elements)}):")
        for el in elements:
            print(f"  {el['tag']} id={el['id']!r} name={el['name']!r} type={el['type']!r} "
                  f"ph={el['placeholder']!r} text={el['text']!r} "
                  f"aria={el['aria-label']!r} visible={el['visible']} "
                  f"rect={el['rect']}")

        # ── Intentar llenar campos ──────────────────────────────────────────
        print("\n[3] Intentando completar login...")

        # El primer input suele ser el de DNI o usuario
        input_els = [e for e in elements if e['tag'] == 'INPUT' and e['visible']]
        print(f"    Inputs visibles: {len(input_els)}")

        # Intentar por texto de label adyacente
        labels_for_inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('label')).map(l => ({
                for: l.htmlFor,
                text: l.textContent.trim().substring(0, 60),
                class: l.className.substring(0, 60)
            }))
        """)
        print(f"    Labels: {labels_for_inputs}")

        # ── Intentar llenar el form con selectores generosos ───────────────
        # Estrategia: usar keyboard navigation
        all_inputs_count = await page.locator("input").count()
        print(f"    Total inputs via locator: {all_inputs_count}")

        if all_inputs_count > 0:
            # Hay inputs, llenarlos
            for i in range(all_inputs_count):
                inp = page.locator("input").nth(i)
                typ = await inp.get_attribute("type") or ""
                ph  = await inp.get_attribute("placeholder") or ""
                iid = await inp.get_attribute("id") or ""
                print(f"    Input[{i}]: type={typ!r} id={iid!r} ph={ph!r}")
        else:
            print("    Sin inputs estándar — buscando inputs por clase")
            # Buscar por clases comunes de input
            for cls_pat in ["input", "Input", "field", "Field", "text-field"]:
                cnt = await page.locator(f"[class*='{cls_pat}'] input, .[class*='{cls_pat}']").count()
                if cnt > 0:
                    print(f"    class*={cls_pat!r}: {cnt}")

        # ── Intentar login si hay inputs ───────────────────────────────────
        print("\n[4] Intentando completar fields...")

        # Probar fill con varios selectores
        selectors_to_try = [
            ("DNI", "input:first-of-type", DNI),
            ("DNI", "input[name*='dni'], input[name*='user'], input[name*='doc']", DNI),
            ("DNI", "input[placeholder*='DNI'], input[placeholder*='dni'], input[placeholder*='documento']", DNI),
            ("DNI", "input[placeholder*='sario'], input[placeholder*='ogin']", DNI),
        ]
        for label, sel, val in selectors_to_try:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    await page.fill(sel, val)
                    print(f"    {label} llenado con {sel!r}")
                    break
            except Exception as e:
                print(f"    {sel!r} fallido: {e}")

        await page.wait_for_timeout(1000)

        # Tomar screenshot para ver el estado
        await page.screenshot(path=str(OUT / "20_form_state.png"), full_page=True)

        # ── Probar navegación directa a secciones ──────────────────────────
        print("\n[5] API responses capturadas:")
        for r in api_responses:
            body = r.get("body", {})
            if isinstance(body, dict):
                data = body.get("data")
                if isinstance(data, dict):
                    info = str(list(data.keys())[:5])
                elif isinstance(data, list):
                    info = f"list({len(data)})"
                else:
                    info = str(data)[:100] if data else str(body.get("message",""))[:80]
            else:
                info = r.get("body_preview", "")[:80]
            print(f"  [{r['status']}] {r['method']} {r['url']}")
            print(f"    {info}")

        # Guardar todos los responses
        with open(OUT / "api_log2.json", "w") as f:
            json.dump(api_responses, f, indent=2, ensure_ascii=False, default=str)

        print(f"\n[6] Screenshots y logs guardados en {OUT}/")
        await page.wait_for_timeout(5000)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

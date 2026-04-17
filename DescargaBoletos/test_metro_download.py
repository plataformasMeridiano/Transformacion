"""
Explorar sección Movimientos en Metrocorp y capturar el endpoint de descarga PDF.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL      = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

# Fecha de prueba — elegir una con movimientos (ayer o días recientes)
FECHA_TEST = "03/03/2026"   # dd/mm/yyyy — hoy
CUENTA = "33460"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

captured: dict[str, dict] = {}
pdf_downloads: list[dict] = []


async def setup_listeners(page):
    async def on_request(req):
        if req.resource_type in ("xhr", "fetch") and "execute" in req.url:
            pd = req.post_data or ""
            if len(pd) > 2:
                print(f"  REQ {req.method} {req.url.split('/')[-1]}")
                print(f"      POST: {pd[:300]}")

    async def on_response(resp):
        if resp.request.resource_type in ("xhr", "fetch"):
            try:
                body = await resp.body()
                ct = resp.headers.get("content-type", "")
                if b"%PDF" in body[:10]:
                    print(f"\n  >>> PDF DESCARGADO: {resp.url} ({len(body)}b)")
                    fpath = OUT / f"boleto_{len(pdf_downloads)}.pdf"
                    fpath.write_bytes(body)
                    pdf_downloads.append({"url": resp.url, "size": len(body), "path": str(fpath)})
                elif "json" in ct and "execute" in resp.url:
                    j = json.loads(body)
                    key = resp.url.split("/")[-1]
                    captured[key] = j
                    if any(x in resp.url for x in ["metrocorp", "movement", "boleto", "download", "comprobante", "voucher"]):
                        data = j.get("data", {})
                        print(f"\n  >>> CAPTURADO: {resp.url}")
                        print(f"      {json.dumps(data, ensure_ascii=False)[:600]}")
            except Exception:
                pass

    page.on("request", on_request)
    page.on("response", on_response)


async def login(page):
    await page.goto(URL, wait_until="networkidle", timeout=60_000)
    await page.wait_for_timeout(3000)
    await page.wait_for_selector("#document\\.number", timeout=15_000)
    await page.fill("#document\\.number", DNI)
    await page.fill("#login\\.step1\\.username", USUARIO)
    await page.click("button[type='submit']:has-text('Continuar')")
    await page.wait_for_selector("#login\\.step2\\.password", timeout=15_000)
    await page.fill("#login\\.step2\\.password", PASSWORD)
    await page.click("button[type='submit']:has-text('Ingresar')")
    await page.wait_for_url(lambda u: "desktop" in u, timeout=25_000)
    await page.wait_for_timeout(3000)
    print(f"[LOGIN] OK — {page.url}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=200)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await setup_listeners(page)
        await login(page)

        # ── Navegar a /metrocorp ──────────────────────────────────────────
        print("\n[1] Navegando a /metrocorp...")
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(3000)

        # ── Click en tab "Movimientos" ─────────────────────────────────────
        print("\n[2] Clickeando tab 'Movimientos'...")
        mov_tab = page.locator("text=/^Movimientos$/i").first
        if await mov_tab.count() > 0:
            await mov_tab.click()
            await page.wait_for_timeout(2000)
            print(f"    Tab Movimientos clickeado")
        else:
            print("    No encontrado, buscando alternativas...")
            tabs = await page.evaluate("""
                () => Array.from(document.querySelectorAll('[role="tab"], .nav-item, .tab, button'))
                    .filter(el => el.offsetParent !== null && el.textContent.trim())
                    .map(el => el.textContent.trim().substring(0,50))
            """)
            print(f"    Tabs disponibles: {tabs}")

        await page.screenshot(path=str(OUT / "60_movimientos.png"), full_page=True)

        # Ver el form actual de Movimientos
        form_state = await page.evaluate("""
            () => {
                const inputs = Array.from(document.querySelectorAll('input, select'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        id: el.id, type: el.type, value: el.value,
                        placeholder: el.placeholder || '',
                        class: el.className.substring(0, 50)
                    }));
                const selects = Array.from(document.querySelectorAll('[class*="Select"], [class*="select"]'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        class: el.className.substring(0, 60),
                        text: el.textContent.trim().substring(0, 50)
                    }));
                return { inputs, selects };
            }
        """)
        print(f"\n    Form state:")
        print(f"    Inputs: {form_state['inputs']}")
        print(f"    Selects: {form_state['selects'][:5]}")

        # ── Configurar fecha y filtrar ─────────────────────────────────────
        print(f"\n[3] Configurando fecha {FECHA_TEST} y filtrando...")

        # Limpiar y poner la fecha
        date_input = page.locator("#date")
        if await date_input.count() > 0:
            await date_input.triple_click()
            await date_input.fill(FECHA_TEST)
            print(f"    Fecha seteada: {FECHA_TEST}")
        else:
            print("    Input #date no encontrado")

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(OUT / "61_fecha_seteada.png"))

        # Click en "Filtrar"
        filtrar_btn = page.locator("button:has-text('Filtrar')").first
        if await filtrar_btn.count() > 0:
            await filtrar_btn.click()
            print("    Filtrar clickeado")
            await page.wait_for_timeout(3000)
        else:
            print("    Botón Filtrar no encontrado")
            buttons = await page.evaluate("""
                () => Array.from(document.querySelectorAll('button'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => el.textContent.trim().substring(0, 40))
            """)
            print(f"    Botones: {buttons}")

        await page.screenshot(path=str(OUT / "62_after_filtrar.png"), full_page=True)

        # ── Ver resultados ────────────────────────────────────────────────
        print(f"\n[4] APIs capturadas hasta ahora: {list(captured.keys())}")

        # Mostrar el contenido de la tabla/resultado
        table_content = await page.evaluate("""
            () => {
                const rows = Array.from(document.querySelectorAll('tr, [class*="row"], [class*="Row"]'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => el.textContent.trim().substring(0, 120));
                const headers = Array.from(document.querySelectorAll('th, [class*="header"], [class*="Header"]'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => el.textContent.trim().substring(0, 50));
                return { rows: rows.slice(0, 20), headers };
            }
        """)
        print(f"\n    Headers: {table_content['headers'][:10]}")
        print(f"    Rows (primeras):")
        for r in table_content['rows'][:10]:
            print(f"      {r!r}")

        # ── Click en PDF ──────────────────────────────────────────────────
        print(f"\n[5] Intentando click en PDF...")
        pdf_btn = page.locator("button:has-text('PDF'), [class*='pdf'], text=/PDF/i").first
        if await pdf_btn.count() > 0:
            btn_text = await pdf_btn.text_content()
            print(f"    Botón PDF encontrado: {btn_text!r}")
            async with context.expect_event("page", timeout=5000) as new_page_info:
                await pdf_btn.click()
            print("    Nueva página abierta")
        else:
            # Intentar expect_download
            print("    Intentando expect_download...")
            try:
                async with page.expect_download(timeout=10_000) as dl_info:
                    await page.locator("text=/PDF/").first.click()
                dl = await dl_info.value
                dest = OUT / f"download_{dl.suggested_filename}"
                await dl.save_as(dest)
                print(f"    Descargado: {dest}")
            except Exception as e:
                print(f"    Error: {e}")
                # Ver botones disponibles
                buttons_after = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('button, a'))
                        .filter(el => el.offsetParent !== null)
                        .map(el => ({
                            tag: el.tagName,
                            text: el.textContent.trim().substring(0, 50),
                            href: el.getAttribute('href') || ''
                        }))
                        .filter(el => el.text)
                """)
                print(f"    Buttons disponibles: {buttons_after[:20]}")

        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT / "63_after_pdf.png"), full_page=True)

        # ── Guardar APIs ───────────────────────────────────────────────────
        with open(OUT / "metro_download_captured.json", "w") as f:
            json.dump({k: v for k, v in captured.items() if "metrocorp" in k or "movement" in k.lower()},
                      f, indent=2, ensure_ascii=False, default=str)

        print(f"\n[6] PDFs descargados: {pdf_downloads}")
        print(f"    APIs con 'metrocorp': {[k for k in captured if 'metrocorp' in k.lower()]}")

        # Dump completo de APIs interesantes
        interesting = {k: v for k, v in captured.items()
                       if any(x in k.lower() for x in ["metrocorp", "boleto", "movement", "voucher", "comprobante", "download"])}
        if interesting:
            print(f"\n    APIs interesantes:")
            for k, v in interesting.items():
                print(f"\n    === {k} ===")
                print(json.dumps(v, indent=2, ensure_ascii=False)[:1500])

        print("\nEsperando 10s...")
        await page.wait_for_timeout(10_000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
Explorar sección Metrocorp — capturar metrocorp.list y metrocorp.list.pre.
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

captured: dict[str, dict] = {}


async def log_responses(page):
    async def on_response(resp):
        if resp.request.resource_type in ("xhr", "fetch") and "execute" in resp.url:
            try:
                body = await resp.body()
                if "json" in resp.headers.get("content-type", ""):
                    j = json.loads(body)
                    key = resp.url.split("/")[-1]
                    captured[key] = j
                    # Imprimir en tiempo real si parece interesante
                    if any(x in resp.url for x in ["metrocorp", "transaction", "histor"]):
                        data = j.get("data", {})
                        print(f"\n  >>> CAPTURADO: {resp.url}")
                        print(f"      {json.dumps(data, ensure_ascii=False)[:500]}")
            except Exception:
                pass
    page.on("response", on_response)


async def login(page):
    await page.goto(URL, wait_until="networkidle", timeout=60_000)
    await page.wait_for_timeout(3000)
    print(f"[LOGIN] URL inicial: {page.url}")

    # Step 1: DNI + usuario
    await page.wait_for_selector("#document\\.number", timeout=15_000)
    await page.fill("#document\\.number", DNI)
    await page.fill("#login\\.step1\\.username", USUARIO)
    await page.click("button[type='submit']:has-text('Continuar')")

    # Esperar step 2 (ya sea por URL o por aparición del campo password)
    try:
        await page.wait_for_selector("#login\\.step2\\.password", timeout=15_000)
        print(f"[LOGIN] Step2 detectado — {page.url}")
    except Exception:
        print(f"[LOGIN] Timeout en step2, URL: {page.url}")
        # Tal vez la página ya está en otro estado
        await page.wait_for_timeout(3000)

    await page.fill("#login\\.step2\\.password", PASSWORD)
    await page.click("button[type='submit']:has-text('Ingresar')")

    # Esperar desktop
    try:
        await page.wait_for_url(lambda u: "desktop" in u, timeout=25_000)
    except Exception:
        await page.wait_for_timeout(5000)

    print(f"[LOGIN] OK — {page.url}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await log_responses(page)
        await login(page)

        # ── Navegar directamente a /metrocorp ──────────────────────────────
        print("\n[1] Navegando a /metrocorp...")
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(5000)

        await page.screenshot(path=str(OUT / "50_metrocorp_direct.png"), full_page=True)
        print(f"    URL: {page.url}")

        # Ver lo que se renderizó
        visible_text = await page.evaluate("""
            () => Array.from(document.querySelectorAll('h1,h2,h3,h4,th,td,label,span,p'))
                .filter(el => el.offsetParent !== null && el.textContent.trim())
                .map(el => el.textContent.trim().substring(0, 80))
                .filter((v, i, a) => a.indexOf(v) === i)
                .slice(0, 40)
        """)
        print(f"\n    Textos visibles:")
        for t in visible_text:
            print(f"      {t!r}")

        # Ver inputs/selects de filtros
        form_elements = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input, select, [role="combobox"]'))
                .filter(el => el.offsetParent !== null)
                .map(el => ({
                    tag: el.tagName,
                    id: el.id,
                    name: el.name || el.getAttribute('name'),
                    type: el.type,
                    placeholder: el.placeholder || el.getAttribute('placeholder'),
                    class: el.className.substring(0, 60),
                    value: el.value ? el.value.substring(0, 30) : ''
                }))
        """)
        print(f"\n    Form elements ({len(form_elements)}):")
        for el in form_elements:
            print(f"      {el}")

        # ── Buscar "Consultar operaciones" ─────────────────────────────────
        print("\n[2] Buscando 'Consultar operaciones'...")
        consult_btn = page.locator("text=/Consultar operaciones/i").first
        if await consult_btn.count() > 0:
            print("    Encontrado! Clickeando...")
            await consult_btn.click()
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(OUT / "51_consultar_ops.png"), full_page=True)
            print(f"    URL: {page.url}")

            # Ver form de filtros
            form_elements2 = await page.evaluate("""
                () => Array.from(document.querySelectorAll('input, select, [role="combobox"]'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        name: el.name || el.getAttribute('name'),
                        type: el.type,
                        placeholder: el.placeholder || el.getAttribute('placeholder') || '',
                        value: el.value ? el.value.substring(0, 30) : ''
                    }))
            """)
            print(f"\n    Form en consultar-ops ({len(form_elements2)}):")
            for el in form_elements2:
                print(f"      {el}")

            # Ver todos los textos
            texts2 = await page.evaluate("""
                () => Array.from(document.querySelectorAll('h1,h2,h3,label,th,button'))
                    .filter(el => el.offsetParent !== null && el.textContent.trim())
                    .map(el => el.textContent.trim().substring(0, 80))
                    .filter((v, i, a) => a.indexOf(v) === i)
            """)
            print(f"\n    Textos en página ({len(texts2)}):")
            for t in texts2:
                print(f"      {t!r}")
        else:
            print("    No encontrado en página. Ver menú...")
            # Clickear el menú Metrocorp
            await page.locator("button").filter(has_text="Metrocorp").first.click()
            await page.wait_for_timeout(1000)
            await page.screenshot(path=str(OUT / "51b_metrocorp_menu.png"))
            # Ahora buscar Consultar operaciones
            consult_btn2 = page.locator("text=/Consultar/i").first
            if await consult_btn2.count() > 0:
                await consult_btn2.click()
                await page.wait_for_timeout(3000)
                print(f"    URL: {page.url}")

        # ── Imprimir todo lo capturado ─────────────────────────────────────
        print(f"\n[3] APIs capturadas con 'execute': {list(captured.keys())}")
        for k, v in captured.items():
            data = v.get("data", {})
            print(f"\n  === {k} ===")
            print(f"  {json.dumps(data, ensure_ascii=False, indent=2)[:1000]}")

        # Guardar
        with open(OUT / "metrocorp_captured.json", "w") as f:
            json.dump(captured, f, indent=2, ensure_ascii=False)

        # Dump HTML de la página actual
        html = await page.evaluate("() => document.body.innerHTML")
        with open(OUT / "metrocorp_body.txt", "w") as f:
            f.write(html)

        print("\n[Esperando 10s...]")
        await page.wait_for_timeout(10_000)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

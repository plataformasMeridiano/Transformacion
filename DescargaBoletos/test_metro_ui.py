"""
Explorar la interacción UI con el DatePicker y el botón Filtrar/PDF en Metrocorp.
También captura headers completos de los requests para entender la autenticación.
"""
import asyncio
import json
import base64
from pathlib import Path

from playwright.async_api import async_playwright

URL      = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

captured_metrocorp: list[dict] = []
auth_headers: dict = {}


async def setup_listeners(page):
    async def on_request(req):
        if req.resource_type in ("xhr", "fetch") and "execute" in req.url:
            key = req.url.split("/")[-1]
            # Capturar todos los headers de los requests a metrocorp
            if "metrocorp" in key:
                hdrs = dict(req.headers)
                auth_headers.update(hdrs)
                print(f"\n  HEADERS de {key}:")
                for h, v in hdrs.items():
                    if h.lower() not in ("accept-encoding", "accept-language", "user-agent", "sec-"):
                        print(f"    {h}: {v[:80]}")

    async def on_response(resp):
        if resp.request.resource_type in ("xhr", "fetch") and "execute" in resp.url:
            key = resp.url.split("/")[-1]
            try:
                body = await resp.body()
                ct = resp.headers.get("content-type", "")
                if "metrocorp" in key and "json" in ct:
                    j = json.loads(body)
                    data = j.get("data", {})
                    captured_metrocorp.append({
                        "key": key,
                        "post_body": resp.request.post_data,
                        "data": data
                    })
                    movs = data.get("movements", [])
                    if movs:
                        print(f"\n  >>> MOVIMIENTOS en {key}: {len(movs)}")
                        for m in movs[:3]:
                            print(f"      {json.dumps(m, ensure_ascii=False)[:200]}")
                    elif b"%PDF" in body[:10]:
                        print(f"\n  >>> PDF en {key}: {len(body)}b")
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
    await page.wait_for_timeout(2000)
    print(f"[LOGIN] OK")


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

        # ── Click en Movimientos ──────────────────────────────────────────
        print("\n[2] Clickeando Movimientos tab...")
        await page.locator("text=/^Movimientos$/i").first.click()
        await page.wait_for_timeout(2000)

        await page.screenshot(path=str(OUT / "80_movimientos.png"), full_page=True)

        # ── Cambiar la fecha con el DatePicker ────────────────────────────
        print("\n[3] Cambiando la fecha en el DatePicker...")
        dates_input = page.locator("#dates")
        if await dates_input.count() > 0:
            current_val = await dates_input.get_attribute("value")
            print(f"    Valor actual: {current_val!r}")

            # Click para abrir el calendario
            await dates_input.click()
            await page.wait_for_timeout(500)

            # Ver el estado del calendario
            cal_html = await page.evaluate("""
                () => {
                    const cal = document.querySelector('.react-datepicker');
                    return cal ? cal.outerHTML.substring(0, 1000) : 'no calendar';
                }
            """)
            print(f"    Calendario HTML: {cal_html[:400]}")
            await page.screenshot(path=str(OUT / "81_calendar_open.png"))

            if cal_html != 'no calendar':
                # Navegar hacia atrás para llegar a febrero 2026
                # El mes actual es marzo 2026, necesito ir a febrero
                prev_btn = page.locator(".react-datepicker__navigation--previous").first
                if await prev_btn.count() > 0:
                    await prev_btn.click()
                    await page.wait_for_timeout(300)
                    await page.screenshot(path=str(OUT / "82_calendar_feb.png"))
                    print("    Navegado a mes anterior")

                # Ver los días disponibles
                day_btns = page.locator(".react-datepicker__day:not(.react-datepicker__day--disabled)")
                day_count = await day_btns.count()
                print(f"    Días disponibles: {day_count}")

                if day_count > 0:
                    # Click en el día 27 (viernes 27/02/2026)
                    day27 = page.locator(".react-datepicker__day[aria-label*='27']").first
                    if await day27.count() > 0:
                        print("    Seleccionando día 27...")
                        await day27.click()
                        await page.wait_for_timeout(300)
                        # Si es range picker, necesitamos clickear un segundo día
                        await day27.click()
                        await page.wait_for_timeout(500)
                    else:
                        # Clickear el primer día disponible
                        await day_btns.first.click()
                        await page.wait_for_timeout(300)
                        await day_btns.first.click()
                        await page.wait_for_timeout(500)

                    await page.screenshot(path=str(OUT / "83_date_selected.png"))
            else:
                # El calendario no apareció, intentar keyboard
                print("    Calendario no abrió — probando keyboard...")
                await dates_input.click(click_count=3)
                await page.keyboard.type("28/02/2026 - 28/02/2026")
                await page.wait_for_timeout(500)

        # Verificar si Filtrar está habilitado
        filtrar_status = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button'))
                .filter(b => b.textContent.trim() === 'Filtrar')
                .map(b => ({ disabled: b.disabled, class: b.className.substring(0,60) }))
        """)
        print(f"\n[4] Filtrar button status: {filtrar_status}")

        current_dates = await page.evaluate("""
            () => {
                const el = document.getElementById('dates');
                return el ? el.value : 'not found';
            }
        """)
        print(f"    Dates input value: {current_dates!r}")

        # ── Intentar click forzado en Filtrar ─────────────────────────────
        print("\n[5] Clickeando Filtrar (forzado)...")
        filtrar_btn = page.locator("button:has-text('Filtrar')").first
        await filtrar_btn.click(force=True)
        await page.wait_for_timeout(4000)
        await page.screenshot(path=str(OUT / "84_after_filtrar.png"), full_page=True)

        # Ver la respuesta API
        print(f"\n    Metrocorp APIs capturadas: {len(captured_metrocorp)}")
        for cap in captured_metrocorp[-3:]:
            data = cap["data"]
            movs = data.get("movements", [])
            post = json.loads(cap["post_body"] or "{}") if cap["post_body"] else {}
            print(f"    {cap['key']}: option={post.get('optionSelected')} movs={len(movs)}")

        # Ver tabla de resultados
        table_content = await page.evaluate("""
            () => {
                const rows = Array.from(document.querySelectorAll('tr'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => el.textContent.trim().replace(/\\s+/g, ' ').substring(0, 120));
                return rows.slice(0, 20);
            }
        """)
        print(f"\n    Tabla ({len(table_content)} rows):")
        for row in table_content:
            print(f"      {row!r}")

        # ── Buscar y click en botón PDF ───────────────────────────────────
        print("\n[6] Buscando botón PDF...")
        buttons = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button'))
                .filter(el => el.offsetParent !== null)
                .map(el => ({
                    text: el.textContent.trim().substring(0, 50),
                    disabled: el.disabled,
                    class: el.className.substring(0, 60)
                }))
                .filter(b => b.text)
        """)
        print(f"    Botones visibles:")
        for b in buttons:
            print(f"      {b['text']!r} disabled={b['disabled']}")

        pdf_btn = page.locator("button:has-text('PDF')").first
        if await pdf_btn.count() > 0:
            is_disabled = await pdf_btn.get_attribute("disabled")
            print(f"    PDF button disabled={is_disabled}")
            print("    Clickeando PDF...")
            try:
                async with page.expect_download(timeout=10_000) as dl_info:
                    await pdf_btn.click(force=True)
                dl = await dl_info.value
                dest = OUT / f"metrocorp_{dl.suggested_filename}"
                await dl.save_as(dest)
                print(f"    *** DESCARGADO: {dest} ***")
            except Exception as e:
                print(f"    expect_download falló: {e}")
                # Ver si se abrió una nueva pestaña
                await pdf_btn.click(force=True)
                await page.wait_for_timeout(3000)

        # ── Ver todas las APIs capturadas con metrocorp ───────────────────
        print("\n[7] Todas las APIs metrocorp capturadas:")
        for cap in captured_metrocorp:
            data = cap["data"]
            post = json.loads(cap["post_body"] or "{}") if cap["post_body"] else {}
            movs = data.get("movements", [])
            print(f"\n  {cap['key']}: option={post.get('optionSelected')} date={post.get('dateFrom')} movs={len(movs)}")
            if movs:
                print(f"  Primer movimiento: {json.dumps(movs[0], ensure_ascii=False, indent=2)[:500]}")

        # Guardar
        with open(OUT / "metro_ui_captured.json", "w") as f:
            json.dump({"metrocorp": captured_metrocorp, "auth_headers": auth_headers},
                      f, indent=2, ensure_ascii=False, default=str)

        await page.screenshot(path=str(OUT / "85_final.png"), full_page=True)
        print("\n[Esperando 10s...]")
        await page.wait_for_timeout(10_000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

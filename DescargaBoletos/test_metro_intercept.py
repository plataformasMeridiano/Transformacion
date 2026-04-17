"""
Intercepta el payload exacto de metrocorp.downloadList cuando se descarga desde el UI.
Navega el datepicker correctamente a febrero, filtra, y captura el request de PDF.
"""
import asyncio
import base64
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL      = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

bearer_token: str = ""
download_requests: list[dict] = []


async def setup_listeners(page):
    global bearer_token

    async def on_request(req):
        global bearer_token
        hdrs = dict(req.headers)
        auth = hdrs.get("authorization", "")
        if auth:
            bearer_token = auth
        if req.resource_type in ("xhr", "fetch") and "execute" in req.url:
            key = req.url.split("/")[-1]
            if key in ("metrocorp.downloadList", "metrocorp.list"):
                pd = req.post_data or ""
                entry = {"endpoint": key, "body": pd}
                download_requests.append(entry)
                print(f"\n  >>> REQUEST {key}: {pd[:300]}")

    async def on_response(resp):
        if "/oauth/token" in resp.url:
            try:
                body = await resp.body()
                j = json.loads(body)
                if "access_token" in j:
                    global bearer_token
                    bearer_token = f"bearer {j['access_token']}"
            except Exception:
                pass
        if "metrocorp.downloadList" in resp.url:
            try:
                body = await resp.body()
                ct = resp.headers.get("content-type", "")
                if b"%PDF" in body[:10]:
                    fname = OUT / f"intercepted.pdf"
                    fname.write_bytes(body)
                    print(f"\n  *** PDF INTERCEPTADO: {len(body)}b → {fname} ***")
                elif "json" in ct:
                    j = json.loads(body)
                    data = j.get("data", {})
                    content = data.get("content", "")
                    filename = data.get("fileName", "")
                    code = j.get("code", "")
                    print(f"\n  RESP downloadList: code={code} fileName={filename!r} content_len={len(content)}")
                    if content:
                        pdf_bytes = base64.b64decode(content)
                        fname = OUT / (filename or "intercepted.pdf")
                        fname.write_bytes(pdf_bytes)
                        print(f"  *** PDF GUARDADO: {fname} ({len(pdf_bytes)}b) ***")
            except Exception as e:
                print(f"  Error procesando downloadList response: {e}")

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


async def select_date_in_picker(page, target_date: str):
    """
    Selecciona una fecha en el React DatePicker range.
    target_date: dd/mm/yyyy
    """
    from datetime import datetime
    target_dt = datetime.strptime(target_date, "%d/%m/%Y")
    target_day = target_dt.day
    target_month = target_dt.month
    target_year = target_dt.year

    # El datepicker muestra el mes actual. Necesitamos navegar al mes correcto.
    # Obtener el mes/año actual del header del calendar
    dates_input = page.locator("#dates")
    await dates_input.click()
    await page.wait_for_timeout(500)

    for attempt in range(12):  # máximo 12 intentos para navegar meses
        # Ver el header actual del calendario
        header_texts = await page.evaluate("""
            () => Array.from(document.querySelectorAll('.react-datepicker__current-month'))
                .map(el => el.textContent.trim())
        """)
        print(f"    Calendar headers: {header_texts}")

        # Determinar el mes/año de la primera vista
        if header_texts:
            # Format puede ser "febrero 2026" o "February 2026" o "2026-02"
            first_header = header_texts[0].lower()
            meses_es = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
                        "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
                        "octubre": 10, "noviembre": 11, "diciembre": 12}
            meses_en = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
                        "june": 6, "july": 7, "august": 8, "september": 9,
                        "october": 10, "november": 11, "december": 12}
            all_months = {**meses_es, **meses_en}

            current_month = None
            current_year = None
            for mes, num in all_months.items():
                if mes in first_header:
                    current_month = num
                    break
            # Extraer año
            import re
            year_match = re.search(r'20\d\d', first_header)
            if year_match:
                current_year = int(year_match.group())

            if current_month is not None and current_year is not None:
                if current_year == target_year and current_month == target_month:
                    # Estamos en el mes correcto!
                    break
                elif (current_year > target_year or
                      (current_year == target_year and current_month > target_month)):
                    # Necesitamos ir hacia atrás
                    prev_btn = page.locator(".react-datepicker__navigation--previous").first
                    await prev_btn.click()
                    await page.wait_for_timeout(300)
                else:
                    # Necesitamos ir hacia adelante
                    next_btn = page.locator(".react-datepicker__navigation--next").first
                    await next_btn.click()
                    await page.wait_for_timeout(300)
            else:
                print(f"    No pude parsear el header: {first_header!r}")
                break
        else:
            print("    No headers de calendario encontrados")
            break

    # Ahora estamos en el mes correcto, click en el día
    await page.screenshot(path=str(OUT / "110_calendar_target.png"))

    # Buscar el día con aria-label que contenga el número del día
    # Diferentes formatos posibles del aria-label
    day_locators = [
        f".react-datepicker__day--0{target_day:02d}:not(.react-datepicker__day--outside-month)",
        f".react-datepicker__day[aria-label*='{target_day}']",
    ]

    day_clicked = False
    for sel in day_locators:
        days = page.locator(sel)
        count = await days.count()
        if count > 0:
            # Tomar el primero que NO sea outside-month
            for i in range(count):
                d = days.nth(i)
                cls = await d.get_attribute("class") or ""
                if "outside-month" not in cls:
                    await d.click()
                    await page.wait_for_timeout(300)
                    # Range picker: click el mismo día como end date también
                    await d.click()
                    await page.wait_for_timeout(300)
                    day_clicked = True
                    print(f"    Día {target_day} clickeado con selector {sel!r}")
                    break
        if day_clicked:
            break

    if not day_clicked:
        # Último recurso: buscar todos los días y filtrar por texto
        all_days = page.locator(".react-datepicker__day:not(.react-datepicker__day--outside-month)")
        count = await all_days.count()
        for i in range(count):
            d = all_days.nth(i)
            txt = await d.text_content()
            if txt and txt.strip() == str(target_day):
                await d.click()
                await page.wait_for_timeout(300)
                await d.click()
                await page.wait_for_timeout(300)
                day_clicked = True
                print(f"    Día {target_day} clickeado por texto")
                break

    # Cerrar el calendario (click fuera)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)

    final_value = await page.locator("#dates").get_attribute("value")
    print(f"    Dates final value: {final_value!r}")
    return day_clicked


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=150)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await setup_listeners(page)
        await login(page)

        # Navegar a metrocorp
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)

        # ── Click en tab Movimientos ──────────────────────────────────────
        print("\n[1] Clickeando Movimientos tab...")
        await page.locator("text=/^Movimientos$/i").first.click()
        await page.wait_for_timeout(1500)

        # ── Seleccionar fecha 25/02/2026 ──────────────────────────────────
        print("\n[2] Seleccionando fecha 25/02/2026...")
        ok = await select_date_in_picker(page, "25/02/2026")
        print(f"    Resultado: {'OK' if ok else 'FAILED'}")

        await page.screenshot(path=str(OUT / "111_date_selected.png"))

        # ── Verificar y clickear Filtrar ──────────────────────────────────
        print("\n[3] Filtrando...")
        filtrar_btn = page.locator("button:has-text('Filtrar')").first
        is_disabled = await filtrar_btn.is_disabled()
        print(f"    Filtrar disabled: {is_disabled}")

        if not is_disabled:
            await filtrar_btn.click()
        else:
            await filtrar_btn.click(force=True)

        await page.wait_for_timeout(4000)
        await page.screenshot(path=str(OUT / "112_filtered.png"), full_page=True)

        # Ver si hay movimientos en la tabla
        table_rows = await page.evaluate("""
            () => Array.from(document.querySelectorAll('tr, [class*="movement"], [class*="row"]'))
                .filter(el => el.offsetParent !== null)
                .map(el => el.textContent.trim().replace(/\\s+/g, ' ').substring(0, 100))
                .filter(t => t.length > 5)
        """)
        print(f"    Filas en tabla: {len(table_rows)}")
        for row in table_rows[:5]:
            print(f"      {row!r}")

        # ── Buscar y clickear el botón de descarga de Movimientos ──────────
        print("\n[4] Buscando botón de descarga para Movimientos...")

        # El botón de descarga puede ser "Descargar", "PDF", o un ícono
        # Hay múltiples "Descargar" - el relevante para Movimientos
        all_btns = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button'))
                .filter(el => el.offsetParent !== null)
                .map((el, i) => ({
                    idx: i,
                    text: el.textContent.trim().substring(0, 50),
                    type: el.type,
                    disabled: el.disabled,
                    class: el.className.substring(0, 60),
                    rect: {
                        top: el.getBoundingClientRect().top,
                        left: el.getBoundingClientRect().left
                    }
                }))
                .filter(b => b.text)
        """)
        print(f"    Botones visibles ({len(all_btns)}):")
        for b in all_btns:
            if any(w in b['text'].lower() for w in ['descargar', 'pdf', 'xls', 'csv', 'download', 'filtrar']):
                print(f"      [{b['idx']}] {b['text']!r} disabled={b['disabled']} class={b['class'][:40]} pos=({b['rect']['top']:.0f},{b['rect']['left']:.0f})")

        # Hacer scroll hacia abajo para ver botones más abajo en la página
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)
        await page.screenshot(path=str(OUT / "113_scrolled.png"), full_page=True)

        # Buscar el "Descargar" de la sección de Movimientos
        # (probablemente el que está más cerca del contenido de movimientos)
        descargar_btns = page.locator("button:has-text('Descargar')")
        count = await descargar_btns.count()
        print(f"\n    Botones 'Descargar': {count}")
        for i in range(count):
            btn = descargar_btns.nth(i)
            try:
                cls = await btn.get_attribute("class") or ""
                is_visible = await btn.is_visible()
                print(f"    [{i}] class={cls[:60]!r} visible={is_visible}")
            except Exception as e:
                print(f"    [{i}] error: {e}")

        # Clickear el "Descargar" que parece ser el dropdown de Movimientos
        # (el primero de la sección visible después de filtrar)
        # Intentar con el que tiene clase 'btn btn-outline download'
        download_dropdown = page.locator("button.download, button[class*='download']").first
        if await download_dropdown.count() > 0:
            await download_dropdown.scroll_into_view_if_needed()
            await download_dropdown.click()
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(OUT / "114_dropdown.png"))
            print(f"    Dropdown clickeado")

            # Ver si apareció el menú PDF/XLS/CSV
            pdf_items = page.locator("text=/PDF/")
            if await pdf_items.count() > 0:
                print(f"    Clickeando PDF en dropdown...")
                try:
                    async with page.expect_download(timeout=15_000) as dl_info:
                        await pdf_items.first.click()
                    dl = await dl_info.value
                    dest = OUT / f"movimientos_{dl.suggested_filename}"
                    await dl.save_as(dest)
                    print(f"    *** DESCARGADO: {dest} ***")
                except Exception as e:
                    print(f"    expect_download falló: {e}")
                    await pdf_items.first.click()
                    await page.wait_for_timeout(5000)

        # ── Ver los download_requests capturados ──────────────────────────
        print(f"\n[5] Download requests capturados: {len(download_requests)}")
        for dr in download_requests:
            print(f"\n  Endpoint: {dr['endpoint']}")
            try:
                body = json.loads(dr['body'])
                print(f"  Body keys: {list(body.keys())}")
                summary = body.get("summary", {})
                if summary:
                    print(f"  Summary keys: {list(summary.keys())}")
                    print(f"  Summary.optionSelected: {summary.get('optionSelected')}")
                    movs = summary.get("movements", [])
                    full_movs = summary.get("fullMovementsList", [])
                    print(f"  movements: {len(movs)}, fullMovementsList: {len(full_movs)}")
                    # Guardar body completo
                    with open(OUT / f"downloadList_body_{dr['endpoint']}.json", "w") as f:
                        json.dump(body, f, indent=2, ensure_ascii=False)
                    print(f"  Body guardado")
            except Exception as e:
                print(f"  Error parseando body: {e}")

        print("\n[Esperando 10s...]")
        await page.wait_for_timeout(10_000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

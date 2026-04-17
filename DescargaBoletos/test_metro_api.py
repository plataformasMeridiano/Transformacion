"""
Llamar directamente a metrocorp.list API con fechas pasadas para ver movimientos
y capturar el endpoint de descarga de PDF.
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

URL      = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

all_api_calls: list[dict] = []


async def setup_listeners(page):
    async def on_request(req):
        if req.resource_type in ("xhr", "fetch") and "execute" in req.url:
            pd = req.post_data or ""
            key = req.url.split("/")[-1]
            if key not in ("messages.listMessages", "session.get", "communications.latestCommunicationThreads",
                           "configuration.listConfiguration", "transactions.get.pending.quantity"):
                print(f"  REQ {key}: {pd[:200]}")

    async def on_response(resp):
        if resp.request.resource_type in ("xhr", "fetch") and "execute" in resp.url:
            try:
                body = await resp.body()
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    j = json.loads(body)
                    key = resp.url.split("/")[-1]
                    entry = {"key": key, "url": resp.url, "body": j}
                    all_api_calls.append(entry)
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


def make_iso_date(date_str: str) -> str:
    """Convierte dd/mm/yyyy a ISO8601 en UTC (medianoche Argentina = 3am UTC)."""
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    # Argentina es UTC-3, medianoche local = 3:00 UTC del mismo día
    dt_utc = dt.replace(hour=3, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def api_post(page, endpoint: str, body: dict) -> dict:
    """Llama a un endpoint via fetch() del browser (usa cookies de sesión del browser)."""
    result = await page.evaluate(
        """async ([url, bodyStr]) => {
            try {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: bodyStr
                });
                const ct = r.headers.get('content-type') || '';
                if (ct.includes('json')) {
                    const j = await r.json();
                    return { ok: true, status: r.status, json: j };
                } else {
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let b64 = '';
                    for (let i=0; i<bytes.byteLength; i++) b64 += String.fromCharCode(bytes[i]);
                    return { ok: true, status: r.status, b64: btoa(b64), ct: ct, len: buf.byteLength };
                }
            } catch(e) {
                return { ok: false, error: e.toString() };
            }
        }""",
        [f"https://be.bancocmf.com.ar/api/v1/execute/{endpoint}", json.dumps(body)]
    )
    return result


async def call_metrocorp_list(page, option: str, fecha: str, especie: str = "all") -> dict:
    """Llama a metrocorp.list con los parámetros dados."""
    iso_date = make_iso_date(fecha)
    body = {
        "optionSelected": option,
        "principalAccount": "33460",
        "species": especie,
        "date": iso_date,
        "dateFrom": iso_date,
        "dateTo": iso_date,
        "page": 1,
        "idEnvironment": 407,
        "lang": "es",
        "channel": "frontend"
    }
    print(f"\n  POST metrocorp.list: option={option} fecha={fecha} especie={especie}")
    result = await api_post(page, "metrocorp.list", body)
    if result.get("ok") and "json" in result:
        return result["json"]
    else:
        print(f"    Error: status={result.get('status')} error={result.get('error')}")
        return {}


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await setup_listeners(page)
        await login(page)

        # ── 1. Probar distintas fechas pasadas para encontrar movimientos ────
        print("\n[1] Probando fechas pasadas para encontrar movimientos...")

        # Fechas recientes de días hábiles
        test_dates = ["28/02/2026", "27/02/2026", "26/02/2026", "25/02/2026", "24/02/2026", "21/02/2026"]

        found_movements = []
        found_date = None

        for fecha in test_dates:
            data = await call_metrocorp_list(page, "movements", fecha)
            movs = data.get("data", {}).get("movements", [])
            total_pages = data.get("data", {}).get("totalPages", 0)
            print(f"  {fecha}: {len(movs)} movimientos, totalPages={total_pages}")
            if movs:
                found_movements = movs
                found_date = fecha
                break

        if not found_movements:
            print("  Sin movimientos en ninguna fecha. Probando rango 01/02-28/02...")
            body = {
                "optionSelected": "movements",
                "principalAccount": "33460",
                "species": "all",
                "date": make_iso_date("21/02/2026"),
                "dateFrom": make_iso_date("01/02/2026"),
                "dateTo": make_iso_date("28/02/2026"),
                "page": 1,
                "idEnvironment": 407,
                "lang": "es",
                "channel": "frontend"
            }
            result = await api_post(page, "metrocorp.list", body)
            if result.get("ok") and "json" in result:
                data = result["json"]
                movs = data.get("data", {}).get("movements", [])
                print(f"  Rango 01/02-28/02: {len(movs)} movimientos")
                if movs:
                    found_movements = movs[:5]
                    found_date = "rango_feb"
                print(f"  data: {json.dumps(data.get('data', {}), ensure_ascii=False)[:600]}")

        # ── 2. Mostrar estructura de movimientos ─────────────────────────
        if found_movements:
            print(f"\n[2] Movimientos encontrados para {found_date} ({len(found_movements)}):")
            for i, m in enumerate(found_movements[:10]):
                print(f"\n  [{i}] {json.dumps(m, indent=4, ensure_ascii=False)}")

        # ── 3. Explorar qué API se llama al hacer click en descarga ─────────
        print("\n[3] Navegando a /metrocorp y explorando descarga PDF/XLS...")
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Click en tab Movimientos
        await page.locator("text=/^Movimientos$/i").first.click()
        await page.wait_for_timeout(1000)

        # Intentar cambiar la fecha usando la UI de React DatePicker
        print("\n[4] Cambiando fecha en el DatePicker...")
        dates_input = page.locator("#dates")
        if await dates_input.count() > 0:
            # El React DatePicker puede responder a click + typing
            await dates_input.click()
            await page.wait_for_timeout(500)

            # Ver si apareció un calendario
            calendar = await page.evaluate("""
                () => {
                    const cal = document.querySelector('[class*="calendar"], [class*="Calendar"], [class*="datepicker"], [class*="DatePicker"]');
                    return cal ? cal.className.substring(0, 80) : null;
                }
            """)
            print(f"    Calendario: {calendar}")
            await page.screenshot(path=str(OUT / "70_datepicker.png"))

            # Intentar escribir directamente en el input
            await dates_input.triple_click()
            await dates_input.type("28/02/2026 - 28/02/2026", delay=50)
            await page.wait_for_timeout(300)

            # Ver si el botón Filtrar se habilitó
            filtrar_disabled = await page.evaluate("""
                () => {
                    const btn = document.querySelector("button[class*='filtrar'], button[class*='filter']");
                    const allBtns = Array.from(document.querySelectorAll('button'))
                        .filter(b => b.textContent.includes('Filtrar') || b.textContent.includes('filtrar'));
                    return allBtns.map(b => ({ text: b.textContent.trim(), disabled: b.disabled }));
                }
            """)
            print(f"    Filtrar buttons: {filtrar_disabled}")

        # ── 4. Intentar cambiar fecha via JavaScript en React ───────────────
        print("\n[5] Intentando cambiar fecha via JS/React...")

        # Probar trigger de eventos en el input de fechas
        await page.evaluate("""
            () => {
                const input = document.getElementById('dates');
                if (!input) { console.log('No dates input'); return; }
                // Simular cambio de valor y triggear eventos React
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeInputValueSetter.call(input, '28/02/2026 - 28/02/2026');
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """)
        await page.wait_for_timeout(1000)

        # Ver si Filtrar está habilitado
        filtrar_status = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button'))
                .filter(b => b.textContent.trim().includes('Filtrar'))
                .map(b => ({ text: b.textContent.trim(), disabled: b.disabled, class: b.className.substring(0,50) }))
        """)
        print(f"    Filtrar status: {filtrar_status}")

        # ── 5. Probar el endpoint de descarga directamente ──────────────────
        print("\n[6] Explorando endpoints de descarga...")

        # El patrón es api/v1/execute/metrocorp.ALGO para descargas
        # Intentar metrocorp.download, metrocorp.list.export, etc.
        test_endpoints = [
            "metrocorp.download",
            "metrocorp.list.download",
            "metrocorp.export",
            "metrocorp.voucher",
            "metrocorp.movements.download",
            "metrocorp.list.export",
        ]
        for ep in test_endpoints:
            body = {
                "optionSelected": "movements",
                "principalAccount": "33460",
                "species": "all",
                "date": make_iso_date("28/02/2026"),
                "dateFrom": make_iso_date("28/02/2026"),
                "dateTo": make_iso_date("28/02/2026"),
                "format": "pdf",
                "idEnvironment": 407,
                "lang": "es",
                "channel": "frontend"
            }
            result = await api_post(page, ep, body)
            if result.get("ok"):
                if "json" in result:
                    j = result["json"]
                    code = j.get("code","")
                    print(f"  {ep}: status={result['status']} code={code} keys={list(j.keys())[:5]}")
                elif "b64" in result:
                    import base64
                    raw = base64.b64decode(result["b64"])
                    print(f"  {ep}: status={result['status']} ct={result['ct']} len={result['len']}")
                    if raw[:4] == b'%PDF':
                        print(f"    *** PDF DESCARGADO! ***")
                        fpath = OUT / f"test_{ep.replace('.','_')}.pdf"
                        fpath.write_bytes(raw)
            else:
                print(f"  {ep}: error={result.get('error')}")

        # ── 6. Ver las URLs de la JS del bundle para encontrar endpoints ────
        print("\n[7] Buscando endpoints de descarga en el HTML post-login...")
        # Buscar en el body de la página strings con "metrocorp" o "download"
        page_source = await page.evaluate("""
            () => {
                // Buscar en todos los scripts
                const scripts = Array.from(document.querySelectorAll('script'))
                    .map(s => s.src || '').filter(s => s);
                return scripts;
            }
        """)
        print(f"    Scripts: {page_source[:5]}")

        # Guardar captura de lo que tenemos
        with open(OUT / "metro_api_captured.json", "w") as f:
            json.dump([
                {k: v for k, v in entry.items() if k != "body"}
                for entry in all_api_calls
            ], f, indent=2, ensure_ascii=False, default=str)

        print("\nEsperando 5s...")
        await page.wait_for_timeout(5_000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

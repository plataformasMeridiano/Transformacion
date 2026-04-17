"""
Test definitivo de descarga de boletos Metrocorp.
Flujo:
  1. Login (captura Bearer token desde oauth/token)
  2. Llama metrocorp.list con fechas pasadas via API directa (usando Bearer token)
  3. Navega a /metrocorp, Movimientos tab, selecciona fecha en calendar (febrero)
  4. Filtrar → captura movimientos
  5. Descarga PDF (botón bulk o individual)
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
captured_metrocorp: list[dict] = []


async def setup_listeners(page):
    global bearer_token

    async def on_request(req):
        global bearer_token
        if req.resource_type in ("xhr", "fetch"):
            hdrs = dict(req.headers)
            auth = hdrs.get("authorization", "")
            if auth.startswith("bearer ") or auth.startswith("Bearer "):
                bearer_token = auth  # guardar el valor completo "bearer TOKEN"
            # Log de metrocorp
            if "metrocorp" in req.url:
                pd = req.post_data or ""
                print(f"  REQ {req.url.split('/')[-1]}: {pd[:200]}")

    async def on_response(resp):
        if resp.request.resource_type in ("xhr", "fetch"):
            try:
                body = await resp.body()
                ct = resp.headers.get("content-type", "")
                # Capturar oauth token también
                if "/oauth/token" in resp.url and "json" in ct:
                    j = json.loads(body)
                    if "access_token" in j:
                        global bearer_token
                        bearer_token = f"bearer {j['access_token']}"
                        print(f"  [OAUTH] Token capturado: {bearer_token[:40]}...")
                elif "metrocorp" in resp.url and "json" in ct:
                    j = json.loads(body)
                    data = j.get("data", {})
                    post_b = resp.request.post_data or "{}"
                    captured_metrocorp.append({
                        "key": resp.url.split("/")[-1],
                        "post": json.loads(post_b) if post_b else {},
                        "data": data
                    })
                    movs = data.get("movements", [])
                    if movs:
                        print(f"\n  >>> MOVIMIENTOS: {len(movs)} en {resp.url.split('/')[-1]}")
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
    print(f"[LOGIN] OK — Bearer: {bearer_token[:40] if bearer_token else 'NOT CAPTURED'}")


async def api_fetch(page, endpoint: str, body: dict) -> dict:
    """Llama API usando fetch() del browser con Bearer token."""
    global bearer_token
    result = await page.evaluate(
        """async ([url, bodyStr, auth]) => {
            try {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json;charset=UTF-8',
                        'Accept': 'application/json, application/octet-stream',
                        'Authorization': auth
                    },
                    credentials: 'include',
                    body: bodyStr
                });
                const ct = r.headers.get('content-type') || '';
                const bodyAb = await r.arrayBuffer();
                const bytes = new Uint8Array(bodyAb);
                // Convert to base64
                let b64 = '';
                const CHUNK = 8192;
                for (let i = 0; i < bytes.byteLength; i += CHUNK) {
                    b64 += String.fromCharCode(...bytes.subarray(i, Math.min(i+CHUNK, bytes.byteLength)));
                }
                return { ok: r.ok, status: r.status, ct: ct, b64: btoa(b64), len: bytes.byteLength };
            } catch(e) {
                return { ok: false, error: e.toString() };
            }
        }""",
        [f"https://be.bancocmf.com.ar/api/v1/execute/{endpoint}", json.dumps(body), bearer_token]
    )
    if not result.get("ok") and result.get("status", 0) != 200:
        return {"error": f"status={result.get('status')} error={result.get('error')}"}
    raw = base64.b64decode(result["b64"])
    ct = result.get("ct", "")
    if b"%PDF" in raw[:10]:
        return {"pdf": raw, "len": len(raw)}
    if "json" in ct:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"raw": raw[:200].decode("utf-8", errors="replace"), "ct": ct, "status": result.get("status")}


def make_iso(date_str: str) -> str:
    """dd/mm/yyyy → ISO UTC (3am = medianoche Argentina)."""
    from datetime import datetime, timezone
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    return dt.replace(hour=3, minute=0, second=0, microsecond=0,
                      tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def call_movements(page, fecha: str) -> list[dict]:
    """Llama metrocorp.list con optionSelected=movements para la fecha dada."""
    iso = make_iso(fecha)
    body = {
        "optionSelected": "movements",
        "principalAccount": "33460",
        "species": "all",
        "date": iso,
        "dateFrom": iso,
        "dateTo": iso,
        "page": 1,
        "idEnvironment": 407,
        "lang": "es",
        "channel": "frontend"
    }
    resp = await api_fetch(page, "metrocorp.list", body)
    if "error" in resp:
        print(f"  {fecha}: error={resp['error']}")
        return []
    data = resp.get("data", {})
    movs = data.get("movements", [])
    total = data.get("totalPages", 0)
    print(f"  {fecha}: {len(movs)} movimientos (totalPages={total})")
    return movs


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

        # Navegar a metrocorp para que el token sea válido en context
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)

        # ── 1. Probar fechas pasadas con token ────────────────────────────
        print(f"\n[1] Probando fechas pasadas (token={bearer_token[:30]}...)")
        test_dates = ["28/02/2026", "27/02/2026", "26/02/2026", "25/02/2026",
                      "24/02/2026", "21/02/2026", "20/02/2026", "19/02/2026"]

        found_movs = []
        found_date = None
        for fecha in test_dates:
            movs = await call_movements(page, fecha)
            if movs:
                found_movs = movs
                found_date = fecha
                break

        if found_movs:
            print(f"\n[2] Movimientos encontrados para {found_date} ({len(found_movs)}):")
            for i, m in enumerate(found_movs[:5]):
                print(f"\n  [{i}] {json.dumps(m, indent=2, ensure_ascii=False)}")
        else:
            print("\n[2] Sin movimientos en ninguna fecha. Guardando respuesta completa...")
            body = {
                "optionSelected": "movements",
                "principalAccount": "33460",
                "species": "all",
                "date": make_iso("21/02/2026"),
                "dateFrom": make_iso("01/02/2026"),
                "dateTo": make_iso("28/02/2026"),
                "page": 1,
                "idEnvironment": 407,
                "lang": "es",
                "channel": "frontend"
            }
            resp = await api_fetch(page, "metrocorp.list", body)
            print(f"  Rango 01/02-28/02: {json.dumps(resp, ensure_ascii=False)[:800]}")
            with open(OUT / "metro_nodata.json", "w") as f:
                json.dump(resp, f, indent=2, ensure_ascii=False, default=str)

        # ── 3. Explorar estructura de un movimiento (si hay) ──────────────
        if found_movs:
            print(f"\n[3] Estructura completa del primer movimiento:")
            print(json.dumps(found_movs[0], indent=2, ensure_ascii=False))

        # ── 4. Navegar el calendar a febrero y usar Filtrar ───────────────
        print("\n[4] Usando UI para seleccionar fecha en Movimientos tab...")
        await page.locator("text=/^Movimientos$/i").first.click()
        await page.wait_for_timeout(1000)

        # Abrir el datepicker
        dates_input = page.locator("#dates")
        await dates_input.click()
        await page.wait_for_timeout(500)

        # Navegar hacia atrás al mes correcto (estamos en marzo, vamos a febrero)
        prev_btn = page.locator(".react-datepicker__navigation--previous").first
        if await prev_btn.count() > 0:
            await prev_btn.click()
            await page.wait_for_timeout(300)
            print("    Navegado a mes anterior (febrero)")

            # Verificar mes actual en el header
            header = await page.locator(".react-datepicker__current-month").first.text_content()
            print(f"    Mes actual: {header!r}")

            await page.screenshot(path=str(OUT / "90_cal_feb.png"))

            # Seleccionar día 27 de febrero
            day27 = page.locator(".react-datepicker__day:not(.react-datepicker__day--outside-month)[aria-label*='27']").first
            if await day27.count() == 0:
                # Buscar con texto exacto
                day27 = page.locator(".react-datepicker__day:not(.react-datepicker__day--outside-month)").filter(has_text="27").first
            if await day27.count() > 0:
                await day27.click()
                await page.wait_for_timeout(300)
                # Para range picker: click en el mismo día como end date
                await day27.click()
                await page.wait_for_timeout(300)
                print("    Día 27/02 seleccionado")
            else:
                print("    Día 27 no encontrado, buscando alternativa...")
                all_days = await page.locator(".react-datepicker__day:not(.react-datepicker__day--outside-month)").all()
                print(f"    Días disponibles: {[await d.text_content() for d in all_days[:10]]}")

        await page.screenshot(path=str(OUT / "91_after_date.png"))
        dates_val = await dates_input.get_attribute("value")
        print(f"    Dates value: {dates_val!r}")

        # Click Filtrar
        filtrar_btn = page.locator("button:has-text('Filtrar')").first
        if not await filtrar_btn.is_disabled():
            print("    Filtrando...")
            await filtrar_btn.click()
            await page.wait_for_timeout(4000)
        else:
            print("    Filtrar sigue disabled, intentando force click...")
            await filtrar_btn.click(force=True)
            await page.wait_for_timeout(4000)

        await page.screenshot(path=str(OUT / "92_filtered.png"), full_page=True)

        # ── 5. Intentar descargar PDF del resultado ───────────────────────
        print("\n[5] Intentando descargar PDF...")

        # Buscar el botón PDF más abajo en la página
        pdf_btn = page.locator("button:has-text('PDF')").last
        if await pdf_btn.count() > 0:
            # Scroll al botón
            await pdf_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(OUT / "93_pdf_btn.png"))

            print("    Clickeando PDF button...")
            try:
                async with page.expect_download(timeout=15_000) as dl_info:
                    await pdf_btn.click()
                dl = await dl_info.value
                dest = OUT / f"boleto_{dl.suggested_filename}"
                await dl.save_as(dest)
                print(f"    *** PDF DESCARGADO: {dest} ***")
            except Exception as e:
                print(f"    expect_download falló: {e}")
                # Intentar de otra forma: capturar request
                await pdf_btn.click(force=True)
                await page.wait_for_timeout(5000)
                print("    (Esperando 5s para ver si se captura algo)")

        # ── 6. Capturar APIs de descarga ───────────────────────────────────
        print("\n[6] APIs capturadas después de filtrar:")
        for cap in captured_metrocorp:
            data = cap["data"]
            movs = data.get("movements", [])
            post = cap["post"]
            print(f"  {cap['key']}: option={post.get('optionSelected')} "
                  f"date={post.get('dateFrom','')} movs={len(movs)}")
            if movs:
                print(f"    Primer mov: {json.dumps(movs[0], ensure_ascii=False)[:300]}")

        with open(OUT / "metro_final_captured.json", "w") as f:
            json.dump(captured_metrocorp, f, indent=2, ensure_ascii=False, default=str)

        print("\n[Esperando 10s...]")
        await page.wait_for_timeout(10_000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
Explorar el tab "Descargar" en Metrocorp y capturar el endpoint de PDF.
También probar endpoints de boleto individual con numeroBoleto/referenciaMinuta.
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

# Fecha con movimientos confirmados
FECHA_TEST = "25/02/2026"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

bearer_token: str = ""
captured: list[dict] = []


async def setup_listeners(page):
    global bearer_token

    async def on_request(req):
        global bearer_token
        hdrs = dict(req.headers)
        auth = hdrs.get("authorization", "")
        if auth:
            bearer_token = auth
        if req.resource_type in ("xhr", "fetch") and "execute" in req.url and "metrocorp" in req.url:
            pd = req.post_data or ""
            print(f"  REQ {req.url.split('/')[-1]}: {pd[:200]}")

    async def on_response(resp):
        if resp.request.resource_type in ("xhr", "fetch") and "execute" in resp.url:
            try:
                body = await resp.body()
                ct = resp.headers.get("content-type", "")
                key = resp.url.split("/")[-1]
                if "metrocorp" in resp.url:
                    post_data = resp.request.post_data or "{}"
                    if "json" in ct:
                        j = json.loads(body)
                        data = j.get("data", {})
                        captured.append({
                            "key": key,
                            "post": json.loads(post_data) if post_data else {},
                            "data_keys": list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                            "code": j.get("code")
                        })
                        print(f"  RESP {key}: code={j.get('code')} data_keys={list(data.keys())[:5] if isinstance(data, dict) else data}")
                    elif b"%PDF" in body[:10]:
                        fname = OUT / f"api_{key}_{len(captured)}.pdf"
                        fname.write_bytes(body)
                        print(f"\n  *** PDF DESCARGADO via API {key}: {len(body)}b → {fname} ***")
                        captured.append({"key": key, "pdf": True, "size": len(body)})
                elif "/oauth/token" in resp.url and "json" in ct:
                    j = json.loads(body)
                    if "access_token" in j:
                        bearer_token = f"bearer {j['access_token']}"
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
    print(f"[LOGIN] OK — Bearer: {bearer_token[:40]}")


async def api_fetch(page, endpoint: str, body: dict):
    """Llama API con Bearer token."""
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
                const bodyAb = await r.arrayBuffer();
                const bytes = new Uint8Array(bodyAb);
                let b64 = '';
                const CHUNK = 8192;
                for (let i = 0; i < bytes.byteLength; i += CHUNK) {
                    b64 += String.fromCharCode(...bytes.subarray(i, Math.min(i+CHUNK, bytes.byteLength)));
                }
                return { ok: r.ok, status: r.status,
                         ct: r.headers.get('content-type') || '',
                         b64: btoa(b64), len: bytes.byteLength };
            } catch(e) {
                return { ok: false, error: e.toString() };
            }
        }""",
        [f"https://be.bancocmf.com.ar/api/v1/execute/{endpoint}", json.dumps(body), bearer_token]
    )
    if not result.get("ok") and result.get("status", 0) not in (200, 201):
        return {"error": f"status={result.get('status')}"}
    raw = base64.b64decode(result["b64"])
    ct = result.get("ct", "")
    if b"%PDF" in raw[:10]:
        return {"pdf": raw}
    if "json" in ct:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"raw": raw[:300].decode("utf-8", errors="replace"), "ct": ct, "status": result.get("status")}


def make_iso(date_str: str) -> str:
    from datetime import datetime, timezone
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    return dt.replace(hour=3, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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

        # Navegar a metrocorp
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(3000)

        # ── 1. Explorar el tab "Descargar" ────────────────────────────────
        print("\n[1] Clickeando tab 'Descargar'...")
        descargar_tabs = page.locator("button:has-text('Descargar')")
        count = await descargar_tabs.count()
        print(f"    Botones Descargar encontrados: {count}")

        # Intentar el que está en la navegación principal (probablemente el primero)
        for i in range(count):
            btn = descargar_tabs.nth(i)
            try:
                cls = await btn.get_attribute("class") or ""
                txt = await btn.text_content() or ""
                print(f"    [{i}] class={cls[:60]!r} text={txt.strip()[:30]!r}")
            except Exception:
                pass

        # Click en el primer "Descargar" (tab principal)
        await descargar_tabs.first.click()
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUT / "100_descargar.png"), full_page=True)
        print(f"    URL: {page.url}")

        # Ver elementos del tab Descargar
        elements = await page.evaluate("""
            () => {
                const visible = Array.from(document.querySelectorAll('input, select, button, label, h1, h2, h3, [class*="Select"]'))
                    .filter(el => el.offsetParent !== null && el.textContent.trim())
                    .map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        type: el.type || '',
                        text: el.textContent.trim().substring(0, 60),
                        placeholder: el.placeholder || el.getAttribute('placeholder') || '',
                        class: el.className.substring(0, 60),
                        value: el.value ? el.value.substring(0, 30) : ''
                    }));
                return visible;
            }
        """)
        print(f"    Elementos en Descargar tab:")
        for el in elements:
            if el['tag'] in ('INPUT', 'SELECT', 'BUTTON', 'LABEL') or 'Select' in el['class']:
                print(f"      {el['tag']} id={el['id']!r} type={el['type']!r} text={el['text']!r} "
                      f"ph={el['placeholder']!r} val={el['value']!r}")

        # ── 2. Llenar el formulario de Descargar ──────────────────────────
        print("\n[2] Llenando formulario de Descargar...")

        # Buscar input de fecha (hay uno para "Fecha de consulta")
        date_inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input'))
                .filter(el => el.offsetParent !== null)
                .map(el => ({ id: el.id, name: el.name, type: el.type, value: el.value, ph: el.placeholder }))
        """)
        print(f"    Inputs visibles: {date_inputs}")

        # Intentar llenar fecha
        for inp_info in date_inputs:
            if 'date' in inp_info.get('id','').lower() or 'fecha' in inp_info.get('ph','').lower():
                sel = f"#{inp_info['id']}" if inp_info['id'] else f"input[placeholder*='{inp_info['ph'][:10]}']"
                await page.fill(sel, FECHA_TEST)
                print(f"    Fecha {FECHA_TEST} llenada en {sel!r}")

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(OUT / "101_descargar_filled.png"))

        # ── 3. Click en PDF del tab Descargar ─────────────────────────────
        print("\n[3] Clickeando PDF en tab Descargar...")
        pdf_btns = page.locator("button:has-text('PDF')")
        pdf_count = await pdf_btns.count()
        print(f"    PDF buttons: {pdf_count}")

        # Scroll a las secciones con PDF
        for i in range(pdf_count):
            btn = pdf_btns.nth(i)
            try:
                await btn.scroll_into_view_if_needed()
                is_visible = await btn.is_visible()
                is_enabled = await btn.is_enabled()
                cls = await btn.get_attribute("class") or ""
                print(f"    PDF[{i}]: visible={is_visible} enabled={is_enabled} class={cls[:60]!r}")
            except Exception as e:
                print(f"    PDF[{i}]: error={e}")

        # Intentar click en PDF habilitado
        for i in range(pdf_count):
            btn = pdf_btns.nth(i)
            try:
                is_enabled = await btn.is_enabled()
                is_visible = await btn.is_visible()
                if is_visible and is_enabled:
                    print(f"    Clickeando PDF[{i}]...")
                    try:
                        async with page.expect_download(timeout=15_000) as dl_info:
                            await btn.click()
                        dl = await dl_info.value
                        dest = OUT / f"descarga_{dl.suggested_filename}"
                        await dl.save_as(dest)
                        print(f"    *** DESCARGADO: {dest} ***")
                        break
                    except Exception as e:
                        print(f"    expect_download falló: {e}")
                        await btn.click(force=True)
                        await page.wait_for_timeout(3000)
            except Exception as e:
                print(f"    Error: {e}")

        await page.screenshot(path=str(OUT / "102_after_pdf.png"), full_page=True)

        # ── 4. Probar endpoint de boleto individual ───────────────────────
        print(f"\n[4] Probando endpoints de boleto individual con numeroBoleto...")
        # Boleto de prueba del 25/02/2026
        test_boleto = "0000004861"
        test_referencia = "11054049P"

        test_ep_bodies = [
            ("metrocorp.voucher", {
                "numeroBoleto": test_boleto,
                "cuentaComitente": "33460",
                "idEnvironment": 407, "lang": "es", "channel": "frontend"
            }),
            ("metrocorp.boleto", {
                "numeroBoleto": test_boleto,
                "cuentaComitente": "33460",
                "idEnvironment": 407, "lang": "es", "channel": "frontend"
            }),
            ("metrocorp.comprobante", {
                "numeroBoleto": test_boleto,
                "idEnvironment": 407, "lang": "es", "channel": "frontend"
            }),
            ("metrocorp.download", {
                "numeroBoleto": test_boleto,
                "referenciaMinuta": test_referencia,
                "format": "pdf",
                "idEnvironment": 407, "lang": "es", "channel": "frontend"
            }),
            ("metrocorp.list.download", {
                "optionSelected": "movements",
                "principalAccount": "33460",
                "numeroBoleto": test_boleto,
                "format": "pdf",
                "dateFrom": make_iso(FECHA_TEST),
                "dateTo": make_iso(FECHA_TEST),
                "idEnvironment": 407, "lang": "es", "channel": "frontend"
            }),
        ]
        for ep, body in test_ep_bodies:
            result = await api_fetch(page, ep, body)
            if "pdf" in result:
                print(f"  *** {ep}: PDF! {len(result['pdf'])}b ***")
                fname = OUT / f"boleto_{ep}.pdf"
                fname.write_bytes(result["pdf"])
                print(f"  Guardado: {fname}")
            elif "error" in result:
                print(f"  {ep}: {result['error']}")
            elif "raw" in result:
                print(f"  {ep}: status={result.get('status')} ct={result.get('ct')} raw={result['raw'][:100]!r}")
            elif isinstance(result, dict):
                print(f"  {ep}: code={result.get('code')} data_keys={list(result.get('data',{}).keys())[:5]}")

        # ── 5. Buscar en el JS del bundle los endpoints de metrocorp ───────
        print("\n[5] Buscando en el HTML los endpoints de metrocorp...")
        # Buscar scripts JS del bundle
        scripts = await page.evaluate("""
            () => Array.from(document.querySelectorAll('script[src]'))
                .map(s => s.src)
                .filter(s => s.includes('static/js'))
        """)
        print(f"    Scripts JS: {scripts[:5]}")

        # ── 6. Dump del HTML del tab Descargar ────────────────────────────
        html = await page.evaluate("() => document.body.innerHTML")
        with open(OUT / "descargar_body.txt", "w") as f:
            f.write(html)
        print(f"\n    HTML guardado ({len(html)} chars)")

        print("\n    APIs capturadas:", [c['key'] for c in captured])
        with open(OUT / "metro_descargar.json", "w") as f:
            json.dump(captured, f, indent=2, ensure_ascii=False, default=str)

        print("\n[Esperando 15s...]")
        await page.wait_for_timeout(15_000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

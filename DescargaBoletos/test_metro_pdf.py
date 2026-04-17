"""
Descarga PDF de movimientos via metrocorp.list + metrocorp.downloadList.
El flujo es:
  1. metrocorp.list (option=movements, fecha) → obtiene los movimientos
  2. metrocorp.downloadList (summary=data_de_lista) → retorna base64 PDF
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

FECHA_TEST = "25/02/2026"  # Fecha con movimientos confirmados

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

bearer_token: str = ""


async def setup_listeners(page):
    global bearer_token

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


async def api_post(page, endpoint: str, body: dict):
    """Llama API via fetch() del browser con Bearer token."""
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
    if not result.get("ok") and result.get("status", 0) not in (200,):
        return {"error": f"status={result.get('status')} err={result.get('error')}"}
    raw = base64.b64decode(result["b64"])
    ct = result.get("ct", "")
    if b"%PDF" in raw[:10]:
        return {"pdf_binary": raw}
    if "json" in ct:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"raw_text": raw[:300].decode("utf-8", errors="replace"), "ct": ct, "status": result.get("status")}


def make_iso(date_str: str) -> str:
    from datetime import datetime, timezone
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    return dt.replace(hour=3, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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

        # Navegar a metrocorp para contexto
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)

        # ── 1. Obtener movimientos de la fecha de prueba ──────────────────
        print(f"\n[1] Obteniendo movimientos para {FECHA_TEST}...")
        iso = make_iso(FECHA_TEST)
        list_body = {
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
        list_resp = await api_post(page, "metrocorp.list", list_body)

        if "error" in list_resp:
            print(f"  Error: {list_resp['error']}")
            return

        data = list_resp.get("data", {})
        movements = data.get("movements", [])
        print(f"  Movimientos encontrados: {len(movements)}")

        for i, m in enumerate(movements):
            desc = m.get("descripcionOperacion", "").strip()
            nro = m.get("numeroBoleto", "")
            ref = m.get("referenciaMinuta", "")
            print(f"  [{i}] {desc!r} boleto={nro} ref={ref}")

        # ── 2. Llamar metrocorp.downloadList con movimientos ──────────────
        print(f"\n[2] Llamando metrocorp.downloadList con option=movements...")
        dl_body = {
            "summary": {
                **data,
                "optionSelected": "movements",
                "principalAccount": "33460",
                "date": iso,
                "dateFrom": iso,
                "dateTo": iso,
            },
            "idEnvironment": 407,
            "lang": "es",
            "channel": "frontend"
        }
        dl_resp = await api_post(page, "metrocorp.downloadList", dl_body)

        if "error" in dl_resp:
            print(f"  Error: {dl_resp['error']}")
        elif "pdf_binary" in dl_resp:
            print(f"  *** PDF binary directo: {len(dl_resp['pdf_binary'])}b ***")
            fname = OUT / f"movimientos_{FECHA_TEST.replace('/','-')}.pdf"
            fname.write_bytes(dl_resp["pdf_binary"])
            print(f"  Guardado: {fname}")
        else:
            print(f"  Respuesta: {json.dumps(dl_resp, ensure_ascii=False)[:500]}")
            # Puede venir como base64 en content
            response_data = dl_resp.get("data", {})
            content_b64 = response_data.get("content", "")
            filename = response_data.get("fileName", "download.pdf")
            code = dl_resp.get("code", "")
            print(f"  code={code} fileName={filename!r} content_len={len(content_b64)}")

            if content_b64:
                # El contenido está en base64
                pdf_bytes = base64.b64decode(content_b64)
                fname = OUT / filename
                fname.write_bytes(pdf_bytes)
                print(f"  *** PDF guardado: {fname} ({len(pdf_bytes)}b) ***")

        # ── 3. Probar también XLS (para ver la estructura de datos) ────────
        print(f"\n[3] Probando XLS del downloadList...")
        dl_body_xls = {
            "summary": {
                **data,
                "optionSelected": "movements",
            },
            "type": "xls",
            "idEnvironment": 407,
            "lang": "es",
            "channel": "frontend"
        }
        xls_resp = await api_post(page, "metrocorp.downloadList", dl_body_xls)
        if "error" in xls_resp:
            print(f"  Error XLS: {xls_resp['error']}")
        else:
            xls_data = xls_resp.get("data", {})
            content_b64 = xls_data.get("content", "")
            filename = xls_data.get("fileName", "download.xls")
            code = xls_resp.get("code", "")
            print(f"  code={code} fileName={filename!r} content_len={len(content_b64)}")
            if content_b64:
                xls_bytes = base64.b64decode(content_b64)
                fname = OUT / filename
                fname.write_bytes(xls_bytes)
                print(f"  XLS guardado: {fname} ({len(xls_bytes)}b)")

        # ── 4. Ver qué pasa con la sección "Resúmenes" ────────────────────
        print(f"\n[4] Explorando endpoint metrocorp.downloadList con Resúmenes...")
        # Probar con optionSelected=summaries o resumes
        for opt in ["summaries", "resumes", "resumenes", "summary"]:
            resp = await api_post(page, "metrocorp.downloadList", {
                "summary": {"optionSelected": opt, "principalAccount": "33460"},
                "idEnvironment": 407, "lang": "es", "channel": "frontend"
            })
            code = resp.get("code", "")
            content = resp.get("data", {}).get("content", "")
            print(f"  opt={opt!r}: code={code} content_len={len(content)}")

        # ── 5. Probar descarga de resumen mensual (sección inferior) ───────
        # La sección inferior tiene "Fecha de consulta *" y PDF/XLS/CSV
        print(f"\n[5] Explorando sección de Resúmenes mensuales...")
        # Esta sección usa otro endpoint probablemente
        for ep in ["metrocorp.downloadSummary", "metrocorp.summary", "metrocorp.resume",
                   "metrocorp.monthlyReport", "metrocorp.report"]:
            resp = await api_post(page, ep, {
                "cuentaComitente": "33460",
                "date": iso,
                "dateFrom": make_iso("01/02/2026"),
                "dateTo": iso,
                "idEnvironment": 407, "lang": "es", "channel": "frontend"
            })
            code = resp.get("code", "")
            content = resp.get("data", {}).get("content", "") if isinstance(resp.get("data"), dict) else ""
            raw = resp.get("raw_text", "")[:80] if "raw_text" in resp else ""
            err = resp.get("error", "")
            print(f"  {ep}: code={code} content={len(content)} raw={raw!r} err={err}")

        print("\nDone!")
        await page.wait_for_timeout(3000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

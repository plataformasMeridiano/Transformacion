"""
Verificación final: descarga completa para 25/02/2026 con datos reales.
"""
import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

URL      = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

bearer_token: str = ""
CUENTA = "33460"
CLIENTE_NOMBRE = "MERIDIANO NORTE SA"


async def setup_oauth(page):
    global bearer_token
    async def on_response(resp):
        if "/oauth/token" in resp.url:
            try:
                j = json.loads(await resp.body())
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
    print(f"[LOGIN] OK")


async def api_post(page, endpoint: str, body: dict):
    global bearer_token
    result = await page.evaluate(
        """async ([url, bodyStr, auth]) => {
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
            for (let i = 0; i < bytes.byteLength; i += CHUNK)
                b64 += String.fromCharCode(...bytes.subarray(i, Math.min(i+CHUNK, bytes.byteLength)));
            return { ok: r.ok, status: r.status,
                     ct: r.headers.get('content-type') || '',
                     b64: btoa(b64), len: bytes.byteLength };
        }""",
        [f"https://be.bancocmf.com.ar/api/v1/execute/{endpoint}", json.dumps(body), bearer_token]
    )
    raw = base64.b64decode(result["b64"])
    if b"%PDF" in raw[:10]:
        return {"pdf_binary": raw}
    if "json" in result.get("ct", ""):
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"raw": raw[:200].decode("utf-8", errors="replace"), "status": result.get("status")}


def make_iso(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    # Midnight Argentina time (UTC-3) = 03:00 UTC
    return dt.replace(hour=3, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def get_movements(page, fecha: str):
    iso = make_iso(fecha)
    body = {
        "optionSelected": "movements",
        "principalAccount": CUENTA,
        "species": "all",
        "date": iso,
        "dateFrom": iso,
        "dateTo": iso,
        "page": 1,
        "idEnvironment": 407,
        "lang": "es",
        "channel": "frontend"
    }
    resp = await api_post(page, "metrocorp.list", body)
    return resp.get("data", {})


async def download_movements_pdf(page, fecha: str, movements_data: dict) -> bytes | None:
    """Descarga el PDF de movimientos del día vía metrocorp.downloadList."""
    iso = make_iso(fecha)
    body = {
        "summary": {
            "activeTotal": movements_data.get("activeTotal", 0),
            "holdings": movements_data.get("holdings", []),
            "futureValues": movements_data.get("futureValues", []),
            "movements": movements_data.get("movements", []),
            "fullMovementsList": movements_data.get("fullMovementsList", []),
            "optionSelected": "movements",
            "filtersData": {
                "principalAccountClient": f" {CLIENTE_NOMBRE}",
                "principalAccount": CUENTA,
                "species": "all",
                "dateFrom": iso,
                "dateTo": iso,
                "page": 1,
            }
        },
        "format": "pdf",
        "idEnvironment": 407,
        "lang": "es",
        "channel": "frontend"
    }
    resp = await api_post(page, "metrocorp.downloadList", body)
    if "pdf_binary" in resp:
        return resp["pdf_binary"]
    data = resp.get("data", {})
    content_b64 = data.get("content", "")
    code = resp.get("code", "")
    print(f"  downloadList: code={code} content_len={len(content_b64)}")
    if content_b64:
        return base64.b64decode(content_b64)
    return None


def classify_movements(movements: list) -> dict:
    """Clasifica movimientos en Cauciones/Pases según descripcionOperacion."""
    result = {"Cauciones": [], "Pases": []}
    CAUCION_KEYWORDS = {"CAUCION", "CAUC", "COLOCACION", "GARANTIA CAUCION"}
    for m in movements:
        desc = m.get("descripcionOperacion", "").upper().strip()
        is_caucion = any(kw in desc for kw in CAUCION_KEYWORDS)
        tipo = "Cauciones" if is_caucion else "Pases"
        result[tipo].append(m)
    return result


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await setup_oauth(page)
        await login(page)

        # Navegar a metrocorp para activar el contexto
        await page.goto("https://be.bancocmf.com.ar/metrocorp", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)

        # ── Verificar con fecha real ──────────────────────────────────────
        test_dates = ["25/02/2026", "27/02/2026", "28/02/2026"]

        for fecha in test_dates:
            print(f"\n{'='*60}")
            print(f"FECHA: {fecha}")
            movements_data = await get_movements(page, fecha)
            movements = movements_data.get("movements", [])
            print(f"Movimientos: {len(movements)}")

            if not movements:
                print("Sin movimientos — skip")
                continue

            # Clasificar
            classified = classify_movements(movements)
            for tipo, movs in classified.items():
                print(f"  {tipo}: {len(movs)}")
                for m in movs:
                    print(f"    [{m.get('numeroBoleto','?')}] {m.get('descripcionOperacion','').strip()!r}")

            # Descargar PDF
            print(f"\nDescargando PDF para {fecha}...")
            pdf_bytes = await download_movements_pdf(page, fecha, movements_data)

            if pdf_bytes and b"%PDF" in pdf_bytes[:10]:
                fname = OUT / f"Movimientos_{fecha.replace('/','')}.pdf"
                fname.write_bytes(pdf_bytes)
                print(f"  *** PDF guardado: {fname} ({len(pdf_bytes)}b) ***")
            else:
                print(f"  ERROR: PDF no obtenido (bytes={pdf_bytes[:20] if pdf_bytes else None})")

        print("\n[Fin]")
        await page.wait_for_timeout(3000)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

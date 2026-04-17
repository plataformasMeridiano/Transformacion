"""
Exploración Max Capital — fase 10:
  - Capturar el Bearer token de las GQL requests
  - Descargar PDFs usando el token + downloadId
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote
from playwright.async_api import async_playwright

URL_LOGIN     = "https://home.max.capital/"
USUARIO       = "40998145"
PASSWORD      = "Meridiano25$"
FECHA         = "2026-02-25"
MOVEMENTS_URL = f"https://home.max.capital/en/account/movements/monetary?from={FECHA}&to={FECHA}&select=OPERATION"
DOWNLOAD_BASE = "https://home.max.capital/backend/api/v1/files/receipts/pdf"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=150)
        context = await browser.new_context()
        page    = await context.new_page()

        # Capturar Bearer token y GQL responses DESDE EL INICIO
        auth_token = None
        gql_movements = None

        async def on_request(req):
            nonlocal auth_token
            hdrs = dict(req.headers)
            if "authorization" in hdrs and hdrs["authorization"].startswith("Bearer"):
                auth_token = hdrs["authorization"]

        async def on_response(resp):
            nonlocal gql_movements
            if "graphql" in resp.url:
                try:
                    req_body = resp.request.post_data or ""
                    parsed = json.loads(req_body) if req_body else {}
                    if parsed.get("operationName") == "getCurrencyTransactionsAccount":
                        body = await resp.json()
                        gql_movements = body
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        # --- Login ---
        print("=== Login ===")
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("#usernameLoginWeb", timeout=30_000)
        await page.fill("#usernameLoginWeb", USUARIO)
        await page.fill("#passwordLoginWeb", PASSWORD)
        await page.click("input[type='submit'], button[type='submit']")
        await page.wait_for_url(lambda u: "sso.max.capital" not in u, timeout=30_000)
        await page.wait_for_timeout(3000)

        # Seleccionar Meridiano Norte Sa
        await page.locator("input").first.click(force=True)
        await page.wait_for_timeout(500)
        await page.locator("button", has_text="Continue").click(force=True)
        await page.wait_for_timeout(3000)
        print(f"Dashboard: {page.url}")
        print(f"Bearer capturado: {'SÍ ('+auth_token[:40]+')' if auth_token else 'NO'}")

        # --- Navegar a movimientos monetarios ---
        print(f"\n=== Navegando a movimientos {FECHA} ===")
        await page.goto(MOVEMENTS_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(5000)

        print(f"Bearer tras navegación: {'SÍ ('+auth_token[:40]+')' if auth_token else 'NO'}")

        if gql_movements is None:
            print("GQL de movimientos no capturado aún")
            await context.close()
            await browser.close()
            return

        # Extraer boletos con downloadId
        movs = gql_movements.get("data", {}).get("currentAccountsMonetary", [])
        boletos = [m for m in movs if m.get("downloadId")]
        print(f"\nBoletos descargables: {len(boletos)}")
        for m in boletos:
            print(f"  detail={m['detail']!r}  downloadId={m['downloadId'][:30]}...")

        if not auth_token:
            print("\nSin Bearer token — no se puede descargar")
            await context.close()
            await browser.close()
            return

        # --- Descargar PDFs via fetch del browser (evita Cloudflare) ---
        Path("downloads/Max_test").mkdir(parents=True, exist_ok=True)
        print(f"\n=== Descargando {len(boletos)} PDFs ===")
        for m in boletos:
            detail = m["detail"]
            did    = m["downloadId"]
            url    = f"{DOWNLOAD_BASE}?downloadId={quote(did, safe='')}"
            print(f"\nDescargando: {detail!r}")

            # Usar fetch desde el browser — tiene cookies Cloudflare + auth token
            result = await page.evaluate(
                """async ([url, token]) => {
                    try {
                        const r = await fetch(url, {
                            headers: { 'Authorization': token },
                            credentials: 'include'
                        });
                        if (!r.ok) return { ok: false, status: r.status, ct: r.headers.get('content-type') };
                        const buf = await r.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        // Convertir a base64
                        let binary = '';
                        for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
                        return { ok: true, status: r.status, ct: r.headers.get('content-type'), b64: btoa(binary), len: buf.byteLength };
                    } catch(e) {
                        return { ok: false, error: e.toString() };
                    }
                }""",
                [url, auth_token]
            )

            print(f"  status={result.get('status')}  CT={result.get('ct')}  len={result.get('len')}")
            if result.get("ok") and result.get("b64"):
                import base64
                body = base64.b64decode(result["b64"])
                if body[:4] == b"%PDF":
                    parts = [p.strip() for p in detail.split("/")]
                    nro   = parts[1] if len(parts) > 1 else str(m["id"])
                    fname = Path(f"downloads/Max_test/BOLETO_{nro}.pdf")
                    fname.write_bytes(body)
                    print(f"  → PDF guardado: {fname} ✓  ({len(body)} bytes)")
                else:
                    print(f"  → No es PDF: {body[:100]}")
            else:
                print(f"  → Error: {result}")

        await context.close()
        await browser.close()
        print("\n=== FIN ===")


if __name__ == "__main__":
    asyncio.run(main())

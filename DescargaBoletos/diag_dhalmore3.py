"""
diag_dhalmore3.py — Explora endpoints de boletos/operaciones en Dhalmore.

Asume que el dispositivo ya está verificado (perfil persistente en browser_profiles/dhalmore/).
Navega a la sección de operaciones y captura los endpoints de boletos.

Uso:
    python3 diag_dhalmore3.py
"""
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(".env"))
import os

USUARIO  = os.environ.get("DHALMORE_USUARIO",  "djoy@meridianonorte.com")
PASSWORD = os.environ.get("DHALMORE_PASSWORD", "")

URL_BASE = "https://clientes.dhalmorecap.com/"
API_BASE = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
OUT_DIR  = Path("downloads/diag_dhalmore3")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = Path("browser_profiles/dhalmore")

from playwright.async_api import async_playwright


async def fetch_json(page, url, bearer, device_id=""):
    result = await page.evaluate(
        """async ([url, auth, deviceId]) => {
            const r = await fetch(url, {
                headers: {
                    'Authorization': auth,
                    'Accept': 'application/json, text/plain, */*',
                    'x-device-id': deviceId,
                    'x-use-wrapped-single-values': 'true',
                    'x-client-name': 'WEB 0.38.2',
                },
                credentials: 'include'
            });
            const text = await r.text();
            return { status: r.status, body: text.slice(0, 3000) };
        }""",
        [url, bearer, device_id],
    )
    print(f"  {result['status']} {url.replace(API_BASE, '')}")
    try:
        return json.loads(result["body"])
    except Exception:
        print(f"    raw: {result['body'][:300]}")
        return {}


async def main():
    bearer = ""
    requests_log = []

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            slow_mo=50,
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        async def on_response(resp):
            nonlocal bearer
            if "/oauth/token" in resp.url:
                try:
                    j = json.loads(await resp.body())
                    if "access_token" in j:
                        bearer = f"Bearer {j['access_token']}"
                        print("  *** Bearer capturado")
                except Exception:
                    pass

        async def on_request(req):
            nonlocal bearer
            if req.resource_type in ("xhr", "fetch") and "fermi" in req.url and "sentry" not in req.url:
                auth = req.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    bearer = auth
                requests_log.append({
                    "method": req.method,
                    "url": req.url,
                    "post": req.post_data,
                    "headers": dict(req.headers),  # capturar TODOS los headers
                })

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── Login ────────────────────────────────────────────────────────────
        print(f"\n[1] Navegando a {URL_BASE}")
        await page.goto(URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3000)
        print(f"  URL: {page.url}")

        if "auth0" in page.url:
            print(f"  Haciendo login...")
            await page.wait_for_selector("input[name='username']", timeout=15_000)
            await page.fill("input[name='username']", USUARIO)
            await page.fill("input[name='password']", PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(4000)

        # Verificar si pide device verification
        code_input = page.locator("input[placeholder*='código' i], input[placeholder*='code' i]").first
        if await code_input.count() > 0:
            print("  ⚠️  Device verification requerida de nuevo!")
            print("  Esperando código en /tmp/dhalmore_code.txt (600s)...")
            Path("/tmp/dhalmore_waiting.txt").write_text("waiting")
            import time
            deadline = time.time() + 600
            while time.time() < deadline:
                if Path("/tmp/dhalmore_code.txt").exists():
                    code = Path("/tmp/dhalmore_code.txt").read_text().strip()
                    Path("/tmp/dhalmore_code.txt").unlink(missing_ok=True)
                    Path("/tmp/dhalmore_waiting.txt").unlink(missing_ok=True)
                    await code_input.fill(code)
                    await page.click("button:has-text('Continuar')")
                    await page.wait_for_timeout(3000)
                    # Click Continuar de confirmación si aparece
                    confirm = page.locator("button:has-text('Continuar')").first
                    if await confirm.count() > 0:
                        await confirm.click()
                        await page.wait_for_timeout(3000)
                    break
                await asyncio.sleep(1)
        else:
            print("  Sin MFA — dispositivo conocido ✓")

        # Click "Continuar" si aparece pantalla de confirmación del dispositivo
        confirm = page.locator("button:has-text('Continuar')").first
        if await confirm.count() > 0:
            print("  Haciendo click en Continuar (confirmación de dispositivo)")
            await confirm.click()
            await page.wait_for_timeout(3000)

        # Esperar que el app cargue completamente y que se renueven los tokens
        await page.wait_for_timeout(8000)
        print(f"  URL final: {page.url}")
        print(f"  Bearer: {'OK' if bearer else 'MISSING'}")
        await page.screenshot(path=str(OUT_DIR / "01_app.png"))

        if not bearer:
            print("  ERROR: Sin bearer")
            await context.close()
            return

        # DeviceId registrado durante la verificación del dispositivo
        device_id = "49dde3e5-bae6-4067-9930-5f213a2468a8"
        print(f"  DeviceId: {device_id}", flush=True)

        # ── Account info ─────────────────────────────────────────────────────
        print(f"\n[2] Account info")
        acc = await fetch_json(page, f"{API_BASE}/account", bearer, device_id)
        uid = acc.get("id", "")
        print(f"  User ID: {uid}")

        # Customer account desde linked-accounts
        linked_url = f"{API_BASE}/linked-accounts/user/{uid.replace('|','%7C')}?page=0&size=20&active.equals=true&type.in=DEFAULT"
        linked = await fetch_json(page, linked_url, bearer, device_id)
        print(f"  Linked: {json.dumps(linked)[:200]}")
        (OUT_DIR / "linked_accounts.json").write_text(json.dumps(linked, indent=2))

        # Si linked_accounts falla, usar el conocido 56553
        customer_account = "56553"
        if isinstance(linked, list) and linked:
            customer_account = str(linked[0].get("customerAccount", {}).get("id", 56553))
        elif isinstance(linked, dict) and linked.get("content"):
            customer_account = str(linked["content"][0].get("customerAccount", {}).get("id", 56553))
        print(f"  Customer account: {customer_account}")

        # ── Explorar endpoints de operaciones/boletos ─────────────────────────
        print(f"\n[3] Explorando endpoints de operaciones ({customer_account})")

        candidates = [
            f"{API_BASE}/operations/customer-account/{customer_account}",
            f"{API_BASE}/operations/customer-account/{customer_account}?page=0&size=20",
            f"{API_BASE}/account-statements/customer-account/{customer_account}",
            f"{API_BASE}/tickets/customer-account/{customer_account}",
            f"{API_BASE}/reports/customer-account/{customer_account}/tickets",
            f"{API_BASE}/customer-accounts/{customer_account}/operations",
            f"{API_BASE}/customer-accounts/{customer_account}/tickets",
            f"{API_BASE}/customer-accounts/{customer_account}/statements",
            f"{API_BASE}/movements/customer-account/{customer_account}",
            f"{API_BASE}/transactions/customer-account/{customer_account}",
        ]
        for url in candidates:
            r = await fetch_json(page, url, bearer, device_id)
            if r and r != {}:
                fname = url.split("/")[-1].split("?")[0]
                (OUT_DIR / f"resp_{fname}.json").write_text(json.dumps(r, indent=2))

        # ── Navegar a sección operaciones en la UI ────────────────────────────
        print(f"\n[4] Navegando a sección operaciones en UI")
        await page.screenshot(path=str(OUT_DIR / "02_home.png"))

        # Buscar link/botón de operaciones/boletos
        for text in ["Operaciones", "Boletos", "Historial", "Movimientos", "Mis operaciones"]:
            el = page.get_by_text(text, exact=False).first
            if await el.count() > 0:
                print(f"  Haciendo click en '{text}'")
                await el.click()
                await page.wait_for_timeout(3000)
                await page.screenshot(path=str(OUT_DIR / f"03_{text.lower()}.png"))
                print(f"  URL: {page.url}")
                break

        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUT_DIR / "04_operaciones.png"))

        # Capturar más requests después de navegar
        print(f"\n[5] Exploración manual (90s) — navegá a Boletos/Operaciones y descargá uno")
        await page.wait_for_timeout(90_000)

        # ── Resumen de requests capturadas ────────────────────────────────────
        print(f"\n[6] Requests capturadas ({len(requests_log)}):")
        for r in requests_log:
            if "sentry" not in r["url"]:
                print(f"  {r['method']} {r['url'].replace(API_BASE, '')[:90]}")
                if r.get("post"):
                    print(f"    {r['post'][:100]}")

        (OUT_DIR / "requests.json").write_text(json.dumps(requests_log, indent=2))
        print(f"\n  Log guardado en requests.json")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

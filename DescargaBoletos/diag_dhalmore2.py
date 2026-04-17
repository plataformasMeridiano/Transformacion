"""
diag_dhalmore2.py — Login completo con perfil persistente + exploración boletos.

Usa un perfil de browser persistente para guardar el deviceId verificado.
Tras el primer login+MFA, las siguientes ejecuciones no piden MFA.

Flujo MFA: escribe el código en /tmp/dhalmore_code.txt para que el script lo lea.

Uso:
    python3 diag_dhalmore2.py
    # En otra terminal, cuando pida el código:
    echo "123456" > /tmp/dhalmore_code.txt
"""
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(".env"))
import os

USUARIO  = os.environ.get("DHALMORE_USUARIO",  "djoy@meridianonorte.com")
PASSWORD = os.environ.get("DHALMORE_PASSWORD", "")

URL_BASE  = "https://clientes.dhalmorecap.com/"
API_BASE  = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
OUT_DIR   = Path("downloads/diag_dhalmore2")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Perfil persistente — guarda localStorage, cookies, deviceId verificado
PROFILE_DIR = Path("browser_profiles/dhalmore")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

CODE_FILE = Path("/tmp/dhalmore_code.txt")

from playwright.async_api import async_playwright


WAITING_FILE = Path("/tmp/dhalmore_waiting.txt")

def wait_for_code(timeout_s: int = 600) -> str:
    """Espera hasta que /tmp/dhalmore_code.txt exista y retorna su contenido."""
    CODE_FILE.unlink(missing_ok=True)
    WAITING_FILE.write_text("waiting")   # señal: el script está listo para el código
    print(f"\n  >>> Escribí el código MFA así:")
    print(f"      echo '123456' > {CODE_FILE}")
    print(f"  (esperando hasta {timeout_s}s...)\n", flush=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if CODE_FILE.exists():
            code = CODE_FILE.read_text().strip()
            CODE_FILE.unlink(missing_ok=True)
            WAITING_FILE.unlink(missing_ok=True)
            return code
        time.sleep(1)
    WAITING_FILE.unlink(missing_ok=True)
    raise TimeoutError("Timeout esperando código MFA")


async def fetch_json(page, url: str, bearer: str) -> dict:
    result = await page.evaluate(
        """async ([url, auth]) => {
            const r = await fetch(url, {
                headers: { 'Authorization': auth, 'Accept': 'application/json' }
            });
            const text = await r.text();
            return { status: r.status, body: text.slice(0, 2000) };
        }""",
        [url, bearer],
    )
    print(f"  {result['status']} {url}")
    try:
        return json.loads(result["body"])
    except Exception:
        print(f"    raw: {result['body'][:200]}")
        return {}


async def main():
    bearer = ""
    requests_log = []

    async with async_playwright() as pw:
        # Perfil persistente — deviceId queda guardado en localStorage
        context = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            slow_mo=80,
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Capturar bearer token
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
            if req.resource_type in ("xhr", "fetch"):
                requests_log.append({
                    "method": req.method,
                    "url": req.url,
                    "post": req.post_data,
                })

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── Login ────────────────────────────────────────────────────────────
        print(f"\n[1] Navegando a {URL_BASE}")
        await page.goto(URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUT_DIR / "01_inicio.png"))
        print(f"  URL: {page.url}")

        # Si ya hay sesión activa (perfil persistente)
        if "clientes.dhalmorecap.com" in page.url and "auth0" not in page.url:
            print("  Sesión existente detectada — saltando login")
        else:
            print(f"\n[2] Login con {USUARIO}")
            await page.wait_for_selector("input[name='username']", timeout=15_000)
            await page.fill("input[name='username']", USUARIO)
            await page.fill("input[name='password']", PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(OUT_DIR / "02_post_submit.png"))
            print(f"  URL post-submit: {page.url}")

        # ── Device verification (MFA) ─────────────────────────────────────────
        # Si aparece el campo de código, hay que verificar el dispositivo
        code_input = page.locator("input[placeholder*='código' i], input[placeholder*='code' i]").first
        if await code_input.count() > 0:
            print(f"\n[3] Device verification requerida")
            code = wait_for_code(timeout_s=600)
            print(f"  Código recibido: {code}")
            await code_input.fill(code)
            await page.click("button:has-text('Continuar')")
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(OUT_DIR / "03_post_mfa.png"))
            print(f"  URL post-MFA: {page.url}")
        else:
            print(f"\n[3] Sin device verification (dispositivo ya conocido)")

        # Esperar a que cargue la app
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT_DIR / "04_app.png"))
        print(f"\n[4] App cargada — URL: {page.url}")
        print(f"  Bearer: {'OK' if bearer else 'MISSING'}")

        if not bearer:
            print("  ERROR: Sin bearer — sesión no establecida")
            await context.close()
            return

        # ── Explorar la API ───────────────────────────────────────────────────
        print(f"\n[5] Explorando API")

        # Account info
        acc = await fetch_json(page, f"{API_BASE}/account", bearer)
        print(f"  Account: {acc.get('login')} — {acc.get('firstName')} {acc.get('lastName')}")

        # Linked accounts (cuentas del usuario)
        uid = acc.get("id", "")
        linked = await fetch_json(
            page,
            f"{API_BASE}/linked-accounts/user/{uid.replace('|', '%7C')}?page=0&size=20&active.equals=true&type.in=DEFAULT",
            bearer,
        )
        print(f"  Linked accounts: {json.dumps(linked)[:300]}")

        # Guardar para análisis
        (OUT_DIR / "account.json").write_text(json.dumps(acc, indent=2))
        (OUT_DIR / "linked_accounts.json").write_text(json.dumps(linked, indent=2))

        # Candidatos de endpoints para boletos/operaciones
        candidates = [
            f"{API_BASE}/reports/tickets",
            f"{API_BASE}/tickets",
            f"{API_BASE}/operations",
            f"{API_BASE}/account/operations",
            f"{API_BASE}/trades",
            f"{API_BASE}/settlements",
            f"{API_BASE}/reports/operations",
            f"{API_BASE}/boletos",
        ]
        print(f"\n[6] Probando endpoints candidatos")
        for url in candidates:
            r = await fetch_json(page, url, bearer)
            if r:
                fname = OUT_DIR / f"resp_{url.split('/')[-1]}.json"
                fname.write_text(json.dumps(r, indent=2))

        # ── Exploración manual ────────────────────────────────────────────────
        print(f"""
========================================================
  EXPLORACIÓN MANUAL (120s)

  Navegá a la sección de Boletos/Cauciones/Pases.
  Hacé click en algún boleto para ver el detalle.
  Mirá DevTools → Network para ver los endpoints.
========================================================
        """)
        await page.wait_for_timeout(120_000)

        # Guardar requests capturadas
        print(f"\n[7] Requests capturadas ({len(requests_log)}):")
        for r in requests_log:
            if "fermi" in r["url"] and "sentry" not in r["url"]:
                print(f"  {r['method']} {r['url'][:100]}")
                if r.get("post"):
                    print(f"    {r['post'][:120]}")

        (OUT_DIR / "requests.json").write_text(
            json.dumps(requests_log, indent=2, ensure_ascii=False)
        )
        print(f"\n  Log guardado en requests.json")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

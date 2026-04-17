"""
diag_dhalmore9.py — Captura URLs exactas de query params y descarga PDF.

Guarda requests a disco en tiempo real (cada request → append a JSONL).
Con eso obtenemos las URLs exactas aunque se mate el proceso.

Uso:
    python3 diag_dhalmore9.py
    # Manual: Actividad → Histórico → Cauciones → fecha → click boleto
    #         luego Pases → fecha → click boleto
"""
import asyncio
import json
import signal
import atexit
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(".env"))
import os

USUARIO     = os.environ.get("DHALMORE_USUARIO",  "djoy@meridianonorte.com")
PASSWORD    = os.environ.get("DHALMORE_PASSWORD", "")
URL_BASE    = "https://clientes.dhalmorecap.com/"
API_BASE    = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
OUT_DIR     = Path("downloads/diag_dhalmore9")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = Path("browser_profiles/dhalmore")

REQUESTS_JSONL = OUT_DIR / "requests.jsonl"   # append en tiempo real
PDF_URLS_FILE  = OUT_DIR / "pdf_urls.txt"     # append en tiempo real

from playwright.async_api import async_playwright


async def main():
    bearer = ""
    pdf_count = [0]

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
            url = resp.url
            if "/oauth/token" in url:
                try:
                    j = json.loads(await resp.body())
                    if "access_token" in j:
                        bearer = f"Bearer {j['access_token']}"
                except Exception:
                    pass

            if "fermi" in url and "sentry" not in url and "websocket" not in url:
                try:
                    body_bytes = await resp.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    path = url.replace(API_BASE, "")
                    is_pdf = body_text.startswith("%PDF") or "application/pdf" in resp.headers.get("content-type", "")
                    print(f"  RESP {resp.status} {'[PDF]' if is_pdf else ''} {path[:100]}")

                    if is_pdf:
                        pdf_count[0] += 1
                        fname = OUT_DIR / f"boleto_{pdf_count[0]}.pdf"
                        fname.write_bytes(body_bytes)
                        with open(PDF_URLS_FILE, "a") as f:
                            f.write(f"{url}\n")
                        print(f"  *** PDF guardado: {fname.name} — URL: {path}")
                    elif resp.status == 200 and body_text.startswith(("[", "{")):
                        short = path.split("?")[0].strip("/").replace("/", "_")[:60].replace("%", "").replace("|", "")
                        try:
                            parsed = json.loads(body_text)
                            (OUT_DIR / f"{short}.json").write_text(
                                json.dumps(parsed, indent=2, ensure_ascii=False))
                        except Exception:
                            pass
                except Exception:
                    pass

        async def on_request(req):
            nonlocal bearer
            if req.resource_type in ("xhr", "fetch") and "fermi" in req.url and "sentry" not in req.url:
                auth = req.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    bearer = auth
                entry = {
                    "method": req.method,
                    "url": req.url,           # URL COMPLETA con query params
                    "post": req.post_data,
                    "headers": {k: v for k, v in req.headers.items()
                                if k.lower() in ("authorization", "x-device-id",
                                                  "x-client-name", "x-use-wrapped-single-values")},
                }
                # Guardar inmediatamente en JSONL
                with open(REQUESTS_JSONL, "a") as f:
                    f.write(json.dumps(entry) + "\n")

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── Login ────────────────────────────────────────────────────────────
        print("[1] Cargando app...")
        await page.goto(URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3000)

        if "auth0" in page.url:
            await page.wait_for_selector("input[name='username']", timeout=15_000)
            await page.fill("input[name='username']", USUARIO)
            await page.fill("input[name='password']", PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(4000)

        code_input = page.locator("input[placeholder*='código' i], input[placeholder*='code' i]").first
        if await code_input.count() > 0:
            print("  ⚠️  MFA!")
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
                    confirm = page.locator("button:has-text('Continuar')").first
                    if await confirm.count() > 0:
                        await confirm.click()
                        await page.wait_for_timeout(3000)
                    break
                await asyncio.sleep(1)
        else:
            print("  Sin MFA ✓")

        await page.wait_for_timeout(6000)
        print(f"  Bearer: {'OK' if bearer else 'MISSING'}")

        # ── Instrucciones y espera ────────────────────────────────────────────
        print(f"""
========================================================
  CAPTURA DE API (300s = 5 minutos)

  Por favor hacé lo siguiente en el browser:

  1. Actividad → Histórico por tipo de Operación
  2. Seleccioná "Cauciones" → seteá una fecha → click ARS/USD
     → el listado carga → hace click en un boleto → PDF
  3. Volvé al combo → seleccioná "Pases" → click ARS/USD
     → hace click en un boleto → PDF

  Todas las URLs quedan guardadas en tiempo real en:
  {REQUESTS_JSONL}
  {PDF_URLS_FILE}
========================================================
        """)
        await page.wait_for_timeout(300_000)

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

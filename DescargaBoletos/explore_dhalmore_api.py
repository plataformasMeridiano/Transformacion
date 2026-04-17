"""
explore_dhalmore_api.py — Captura el endpoint de "Operaciones del día" de Dhalmore.

Abre el browser, hace login si es necesario, y loguea todos los requests a la API
de Fermi mientras navegás manualmente a "Operaciones del día" y seleccionás una fecha.
"""
import asyncio
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(Path(__file__).parent / ".env")

_PROFILE_DIR = Path("browser_profiles/dhalmore")
_API_BASE    = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
_URL_BASE    = "https://clientes.dhalmorecap.com/"
_USUARIO     = os.environ.get("DHALMORE_USUARIO", "")
_PASSWORD    = os.environ.get("DHALMORE_PASSWORD", "")


async def main():
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,
            slow_mo=50,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        captured = []

        async def on_request(req):
            if _API_BASE in req.url and req.resource_type in ("xhr", "fetch"):
                entry = {"method": req.method, "url": req.url}
                try:
                    entry["post_data"] = req.post_data
                except Exception:
                    pass
                captured.append(entry)
                print(f"\n>>> REQUEST  {req.method} {req.url}")
                if entry.get("post_data"):
                    print(f"    BODY: {entry['post_data'][:500]}")

        async def on_response(resp):
            if _API_BASE in resp.url and resp.request.resource_type in ("xhr", "fetch"):
                try:
                    body = await resp.body()
                    text = body[:2000].decode("utf-8", errors="replace")
                    if any(k in text for k in ("operationDate", "documentKey", "orderCode", "content", "operation")):
                        print(f"\n>>> RESPONSE {resp.status} {resp.url}")
                        print(f"    {text[:800]}")
                except Exception:
                    pass

        page.on("request",  on_request)
        page.on("response", on_response)

        await page.goto(_URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(2000)

        # Login automático si redirigió a auth0
        if "auth0" in page.url:
            print("[login] Detectado auth0 — haciendo login...")
            await page.wait_for_selector("input[name='username']", timeout=15_000)
            await page.fill("input[name='username']", _USUARIO)
            await page.fill("input[name='password']", _PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(5000)
            print(f"[login] URL post-login: {page.url}")

        print("\n=== Listo ===")
        print("1. Navegá al menú 'Actividad > Histórico tenencia'")
        print("2. Filtrá desde/hasta 2026-01-26")
        print("3. Esperá que carguen los resultados")
        print("4. Intentá descargar el boleto de la FCE (Venta Cheques MAV)")
        print("5. Cerrá el browser cuando termines\n")

        try:
            await context.wait_for_event("close", timeout=600_000)
        except Exception:
            pass

        out = Path("dhalmore_api_capture.json")
        out.write_text(json.dumps(captured, indent=2, ensure_ascii=False))
        print(f"\nCapturados {len(captured)} requests → {out}")


if __name__ == "__main__":
    asyncio.run(main())

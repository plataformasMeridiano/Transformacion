"""
diag_dhalmore4.py — Captura respuestas de API al navegar a Actividad/Operaciones.

Estrategia: interceptar response BODIES de la app en lugar de replicar fetch calls.
Navega automáticamente a la sección "Actividad" y captura todos los endpoints.

Uso:
    python3 diag_dhalmore4.py
"""
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(".env"))
import os

USUARIO  = os.environ.get("DHALMORE_USUARIO",  "djoy@meridianonorte.com")
PASSWORD = os.environ.get("DHALMORE_PASSWORD", "")

URL_BASE    = "https://clientes.dhalmorecap.com/"
API_BASE    = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
OUT_DIR     = Path("downloads/diag_dhalmore4")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = Path("browser_profiles/dhalmore")

from playwright.async_api import async_playwright


async def main():
    bearer = ""
    responses_captured = {}   # url → {status, body}
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
            url = resp.url
            if "/oauth/token" in url:
                try:
                    j = json.loads(await resp.body())
                    if "access_token" in j:
                        bearer = f"Bearer {j['access_token']}"
                        print("  *** Bearer capturado")
                except Exception:
                    pass

            # Capturar response bodies de la API
            if "fermi" in url and "sentry" not in url and "websocket" not in url:
                try:
                    body_bytes = await resp.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    path = url.replace(API_BASE, "").split("?")[0]
                    print(f"  RESP {resp.status} {path[:80]}")
                    if resp.status == 200 and body_text.startswith(("[", "{")):
                        short_key = path.strip("/").replace("/", "_")
                        responses_captured[url] = {
                            "status": resp.status,
                            "path": path,
                            "body": body_text[:5000],
                        }
                        # Guardar respuestas útiles
                        fname = short_key[:60].replace("%", "").replace("|", "") + ".json"
                        try:
                            parsed = json.loads(body_text)
                            (OUT_DIR / fname).write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
                        except Exception:
                            pass
                except Exception as e:
                    pass  # response ya consumida o streaming

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
                    "headers": {k: v for k, v in req.headers.items()
                                if k.lower() in ("authorization", "x-device-id", "x-client-name",
                                                  "x-use-wrapped-single-values", "accept")},
                })

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── Login ────────────────────────────────────────────────────────────
        print(f"\n[1] Navegando a {URL_BASE}")
        await page.goto(URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3000)
        print(f"  URL: {page.url}")

        if "auth0" in page.url:
            print("  Haciendo login...")
            await page.wait_for_selector("input[name='username']", timeout=15_000)
            await page.fill("input[name='username']", USUARIO)
            await page.fill("input[name='password']", PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(4000)

        # MFA
        code_input = page.locator("input[placeholder*='código' i], input[placeholder*='code' i]").first
        if await code_input.count() > 0:
            print("  ⚠️  Device verification requerida!")
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
                    confirm = page.locator("button:has-text('Continuar')").first
                    if await confirm.count() > 0:
                        await confirm.click()
                        await page.wait_for_timeout(3000)
                    break
                await asyncio.sleep(1)
        else:
            print("  Sin MFA — dispositivo conocido ✓")

        confirm = page.locator("button:has-text('Continuar')").first
        if await confirm.count() > 0:
            await confirm.click()
            await page.wait_for_timeout(3000)

        await page.wait_for_timeout(8000)
        print(f"  URL: {page.url}")
        print(f"  Bearer: {'OK' if bearer else 'MISSING'}")
        await page.screenshot(path=str(OUT_DIR / "01_app.png"))

        if not bearer:
            print("  ERROR: Sin bearer")
            await context.close()
            return

        # ── Navegar a Actividad ───────────────────────────────────────────────
        print(f"\n[2] Buscando sección Actividad/Operaciones")
        await page.screenshot(path=str(OUT_DIR / "02_home.png"))

        # Intentar Actividad primero (lo que se vio en el screenshot anterior)
        clicked = False
        for text in ["Actividad", "Operaciones", "Boletos", "Historial", "Movimientos"]:
            el = page.get_by_role("link", name=text).first
            if await el.count() == 0:
                el = page.get_by_text(text, exact=True).first
            if await el.count() > 0:
                print(f"  Haciendo click en '{text}'")
                await el.click()
                await page.wait_for_timeout(4000)
                await page.screenshot(path=str(OUT_DIR / f"03_{text.lower()}.png"))
                print(f"  URL: {page.url}")
                clicked = True
                break

        if not clicked:
            print("  No se encontró link de navegación — esperando manualmente 30s")
            await page.wait_for_timeout(30_000)

        # Esperar que se carguen las requests
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT_DIR / "04_actividad.png"))

        # ── Intentar filtrar por fecha ────────────────────────────────────────
        print(f"\n[3] Buscando controles de fecha/filtro")

        # Ver qué hay en la página
        html_text = await page.evaluate("() => document.body.innerText")
        (OUT_DIR / "actividad_text.txt").write_text(html_text[:3000])
        print(f"  Texto visible (primeros 500 chars): {html_text[:500]}")

        # Buscar inputs de fecha
        date_inputs = await page.query_selector_all("input[type='date'], input[placeholder*='fecha' i], input[placeholder*='from' i], input[placeholder*='date' i]")
        print(f"  Date inputs encontrados: {len(date_inputs)}")

        # ── Hacer scroll para cargar más ──────────────────────────────────────
        print(f"\n[4] Scrolling para cargar más items")
        for _ in range(3):
            await page.keyboard.press("End")
            await page.wait_for_timeout(1500)

        await page.screenshot(path=str(OUT_DIR / "05_scroll.png"))

        # ── Intentar hacer click en un item para ver detalle ──────────────────
        print(f"\n[5] Intentando abrir detalle de operación")
        await page.wait_for_timeout(2000)

        # Buscar filas de la tabla o items de lista
        rows = await page.query_selector_all("tr[role='row'], tr:not(:first-child), .operation-row, .activity-item, li.item")
        print(f"  Rows/items encontrados: {len(rows)}")
        if rows:
            print("  Haciendo click en primer item")
            await rows[0].click()
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(OUT_DIR / "06_detalle.png"))

        # ── Exploración manual adicional (60s) ────────────────────────────────
        print(f"\n[6] Exploración manual (60s) — navegá a distintos boletos")
        await page.wait_for_timeout(60_000)

        # ── Resumen ───────────────────────────────────────────────────────────
        print(f"\n[7] Responses capturadas ({len(responses_captured)}):")
        for url, info in responses_captured.items():
            path = url.replace(API_BASE, "")
            print(f"  {info['status']} {path[:90]}")
            # Mostrar preview de la estructura
            try:
                data = json.loads(info["body"])
                if isinstance(data, list):
                    print(f"    → lista de {len(data)} items")
                    if data:
                        print(f"    → keys[0]: {list(data[0].keys()) if isinstance(data[0], dict) else data[0]}")
                elif isinstance(data, dict):
                    print(f"    → keys: {list(data.keys())[:10]}")
                    if "content" in data:
                        print(f"    → content[0]: {list(data['content'][0].keys()) if data['content'] and isinstance(data['content'][0], dict) else ''}")
            except Exception:
                pass

        print(f"\n[8] Requests capturadas ({len(requests_log)}):")
        for r in requests_log:
            print(f"  {r['method']} {r['url'].replace(API_BASE, '')[:90]}")
            if r.get("post"):
                print(f"    {r['post'][:100]}")

        (OUT_DIR / "responses.json").write_text(json.dumps(responses_captured, indent=2, ensure_ascii=False))
        (OUT_DIR / "requests.json").write_text(json.dumps(requests_log, indent=2, ensure_ascii=False))
        print(f"\n  Guardado en {OUT_DIR}/")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

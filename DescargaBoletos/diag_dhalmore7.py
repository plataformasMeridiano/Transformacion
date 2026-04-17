"""
diag_dhalmore7.py — Navega a Actividad → Histórico por tipo de Operación → Cauciones.

Captura los endpoints de la API para cauciones (y pases) con filtro de fechas.

Uso:
    python3 diag_dhalmore7.py
"""
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(".env"))
import os

USUARIO     = os.environ.get("DHALMORE_USUARIO",  "djoy@meridianonorte.com")
PASSWORD    = os.environ.get("DHALMORE_PASSWORD", "")
URL_BASE    = "https://clientes.dhalmorecap.com/"
API_BASE    = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
OUT_DIR     = Path("downloads/diag_dhalmore7")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = Path("browser_profiles/dhalmore")

# Fecha de prueba
TEST_DATE   = "2026-03-05"   # ajustar a una fecha con datos

from playwright.async_api import async_playwright


async def main():
    bearer = ""
    device_id = "49dde3e5-bae6-4067-9930-5f213a2468a8"
    responses_captured = {}
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
            if "fermi" in resp.url and "sentry" not in resp.url and "websocket" not in resp.url:
                try:
                    body_bytes = await resp.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    path = resp.url.replace(API_BASE, "").split("?")[0]
                    print(f"    RESP {resp.status} {path[:80]}")
                    if resp.status == 200 and len(body_text) > 5:
                        responses_captured[resp.url] = {
                            "status": resp.status, "path": path,
                            "query": resp.url.split("?", 1)[1] if "?" in resp.url else "",
                            "body": body_text[:10000],
                        }
                        if body_text.startswith(("[", "{")):
                            short = path.strip("/").replace("/", "_")[:60].replace("%7C", "").replace("%", "").replace("|", "")
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
                requests_log.append({
                    "method": req.method,
                    "url": req.url,
                    "post": req.post_data,
                    "headers": {k: v for k, v in req.headers.items()
                                if k.lower() in ("authorization", "x-device-id", "x-client-name",
                                                  "x-use-wrapped-single-values", "accept", "content-type")},
                })

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── Login ────────────────────────────────────────────────────────────
        print("[1] Cargando app...")
        await page.goto(URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3000)
        print(f"  URL: {page.url}")

        if "auth0" in page.url:
            print("  Login...")
            await page.wait_for_selector("input[name='username']", timeout=15_000)
            await page.fill("input[name='username']", USUARIO)
            await page.fill("input[name='password']", PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(4000)

        code_input = page.locator("input[placeholder*='código' i], input[placeholder*='code' i]").first
        if await code_input.count() > 0:
            print("  ⚠️  MFA requerido!")
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
        print(f"  URL: {page.url}, Bearer: {'OK' if bearer else 'MISSING'}")

        # ── Actividad ─────────────────────────────────────────────────────────
        print("\n[2] Navegando a Actividad")
        await page.screenshot(path=str(OUT_DIR / "01_home.png"))

        actividad_el = page.get_by_text("Actividad", exact=True).first
        if await actividad_el.count() > 0:
            await actividad_el.click()
            await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT_DIR / "02_actividad.png"))
        print(f"  URL: {page.url}")
        text = await page.evaluate("() => document.body.innerText.slice(0, 1000)")
        print(f"  Texto: {text[:400]}")

        # ── Histórico por tipo de Operación ───────────────────────────────────
        print("\n[3] Buscando 'Histórico por tipo de Operación'")

        # Buscar por texto
        hist_el = page.get_by_text("Histórico por tipo de Operación", exact=False).first
        if await hist_el.count() == 0:
            hist_el = page.get_by_text("Histórico", exact=False).first
        if await hist_el.count() == 0:
            hist_el = page.get_by_text("tipo de Operación", exact=False).first
        if await hist_el.count() == 0:
            hist_el = page.get_by_text("por tipo", exact=False).first

        if await hist_el.count() > 0:
            el_text = await hist_el.inner_text()
            print(f"  Encontrado: {el_text!r}")
            await hist_el.click()
            await page.wait_for_timeout(3000)
        else:
            print("  No encontrado directamente — dumpear sidebar")
            sidebar_text = await page.evaluate("() => document.body.innerText.slice(0, 2000)")
            print(sidebar_text[:800])

        await page.screenshot(path=str(OUT_DIR / "03_historico.png"))
        text = await page.evaluate("() => document.body.innerText.slice(0, 2000)")
        (OUT_DIR / "03_historico_text.txt").write_text(text)
        print(f"  URL: {page.url}")
        print(f"  Texto: {text[:400]}")

        # ── Seleccionar "Cauciones" en Tipo de Reporte ────────────────────────
        print("\n[4] Seleccionando Cauciones en Tipo de Reporte")

        # Buscar el combo/select de Tipo de Reporte
        await page.wait_for_timeout(1000)

        # Intentar varias formas de encontrar el combo
        tipo_el = None
        for selector in [
            "text=Tipo de Reporte",
            "text=Cauciones",
            "select",
            "[role='combobox']",
            "[role='listbox']",
        ]:
            el = page.locator(selector).first
            if await el.count() > 0:
                print(f"  Encontrado selector: {selector!r}")
                tipo_el = el
                break

        # Intentar buscar todos los elementos select/combo de la página
        selects_info = await page.evaluate("""() => {
            const results = [];
            // Selects nativos
            document.querySelectorAll('select').forEach(el => {
                results.push({
                    type: 'select',
                    id: el.id,
                    name: el.name,
                    options: Array.from(el.options).map(o => o.text),
                    classes: el.className?.slice(0, 50)
                });
            });
            // MUI selects / combobox
            document.querySelectorAll('[role="combobox"], [role="listbox"]').forEach(el => {
                results.push({
                    type: el.role,
                    text: el.innerText?.trim()?.slice(0, 80),
                    classes: el.className?.slice(0, 50)
                });
            });
            // Elementos con texto "Cauciones"
            const caucs = document.querySelectorAll('*');
            caucs.forEach(el => {
                if (el.innerText?.trim() === 'Cauciones') {
                    results.push({
                        type: 'caucion-el',
                        tag: el.tagName,
                        text: el.innerText?.trim(),
                        classes: el.className?.slice(0, 50),
                        role: el.getAttribute('role')
                    });
                }
            });
            return results;
        }""")
        print(f"  Selects/combos encontrados: {len(selects_info)}")
        for s in selects_info:
            print(f"    {s}")
        (OUT_DIR / "selects_info.json").write_text(json.dumps(selects_info, indent=2, ensure_ascii=False))

        # Capturar texto completo de la página en este punto
        full_text = await page.evaluate("() => document.body.innerText.slice(0, 3000)")
        (OUT_DIR / "04_before_caucion_text.txt").write_text(full_text)

        # Intentar click en "Cauciones" si ya está visible
        caucion_el = page.get_by_text("Cauciones", exact=True).first
        if await caucion_el.count() > 0:
            print("  Haciendo click en 'Cauciones'")
            await caucion_el.click()
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(OUT_DIR / "05_caucion_selected.png"))
            text = await page.evaluate("() => document.body.innerText.slice(0, 2000)")
            (OUT_DIR / "05_caucion_text.txt").write_text(text)
            print(f"  Texto post-Caucion: {text[:400]}")

        # ── Filtrar por fecha y ver resultados ────────────────────────────────
        print(f"\n[5] Intentando setear fecha {TEST_DATE}")

        # Buscar inputs de fecha
        date_els = await page.query_selector_all("input[type='date'], input[placeholder*='fecha' i], input[placeholder*='Desde' i], input[placeholder*='Hasta' i]")
        print(f"  Date inputs: {len(date_els)}")
        for i, el in enumerate(date_els):
            ph = await el.get_attribute("placeholder") or ""
            typ = await el.get_attribute("type") or ""
            val = await el.get_attribute("value") or ""
            print(f"    [{i}] type={typ} placeholder={ph!r} value={val!r}")

        await page.screenshot(path=str(OUT_DIR / "06_pre_date.png"))

        # ── Exploración manual extendida ─────────────────────────────────────
        print("""
========================================================
  EXPLORACIÓN MANUAL (180s)

  Por favor:
  1. Navegá a Actividad → Histórico por tipo de Operación
  2. Seleccioná "Cauciones" en Tipo de Reporte
  3. Ponés una fecha y hacés click en ARS/USD
  4. Hacé click en algún boleto individual

  Los API calls serán capturados automáticamente.
========================================================
        """)
        await page.wait_for_timeout(180_000)

        # ── Resumen ───────────────────────────────────────────────────────────
        print(f"\n[6] API calls capturados ({len(requests_log)}):")
        seen = set()
        for r in requests_log:
            path = r['url'].replace(API_BASE, "").split("?")[0]
            key = r['method'] + " " + path
            if key not in seen:
                seen.add(key)
                print(f"  {r['method']} {path}")
                if r.get("post"):
                    print(f"    POST body: {r['post'][:200]}")

        print(f"\n[7] Responses capturadas ({len(responses_captured)}):")
        for url, info in responses_captured.items():
            path = url.replace(API_BASE, "").split("?")[0]
            try:
                d = json.loads(info["body"])
                if isinstance(d, list):
                    s = f"lista={len(d)} items"
                    if d and isinstance(d[0], dict):
                        s += f", keys={list(d[0].keys())[:6]}"
                elif isinstance(d, dict):
                    s = f"keys={list(d.keys())[:6]}"
                    if "content" in d and d["content"]:
                        s += f", content[0].keys={list(d['content'][0].keys())[:6]}"
                else:
                    s = "?"
            except Exception:
                s = info["body"][:80]
            print(f"  {info['status']} {path[:70]}")
            if info.get("query"):
                print(f"    query: {info['query'][:100]}")
            print(f"    {s}")

        (OUT_DIR / "responses.json").write_text(json.dumps(responses_captured, indent=2, ensure_ascii=False))
        (OUT_DIR / "requests.json").write_text(json.dumps(requests_log, indent=2, ensure_ascii=False))
        print(f"\n  Guardado en {OUT_DIR}/")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
diag_dhalmore8.py — Captura query params exactos y endpoint de descarga de PDF.

Flujo automático:
  1. Login → Actividad → Histórico por tipo de Operación
  2. Selecciona Cauciones, filtra por fecha 2026-03-05
  3. Hace click en el primer boleto → captura endpoint de descarga
  4. Repite para Pases

Uso:
    python3 diag_dhalmore8.py
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
OUT_DIR     = Path("downloads/diag_dhalmore8")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = Path("browser_profiles/dhalmore")

TEST_DATE   = "2026-03-05"

from playwright.async_api import async_playwright


async def main():
    bearer = ""
    requests_log = []   # lista completa (con URL completa + query)
    responses_captured = {}
    downloads_seen = []

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
                except Exception:
                    pass
            if "fermi" in resp.url and "sentry" not in resp.url and "websocket" not in resp.url:
                try:
                    body_bytes = await resp.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    path = resp.url.replace(API_BASE, "")
                    print(f"    RESP {resp.status} {path[:100]}")
                    if resp.status == 200 and len(body_text) > 5:
                        responses_captured[resp.url] = {
                            "status": resp.status,
                            "path": path.split("?")[0],
                            "query": path.split("?", 1)[1] if "?" in path else "",
                            "body": body_text[:12000],
                        }
                        if body_text.startswith(("[", "{")):
                            short = path.split("?")[0].strip("/").replace("/", "_")[:70].replace("%", "").replace("|", "")
                            try:
                                parsed = json.loads(body_text)
                                (OUT_DIR / f"{short}.json").write_text(
                                    json.dumps(parsed, indent=2, ensure_ascii=False))
                            except Exception:
                                pass
                        # Detectar PDFs
                        if body_text.startswith("%PDF") or "application/pdf" in resp.headers.get("content-type", ""):
                            print(f"    *** PDF DETECTADO en {path}")
                            downloads_seen.append(resp.url)
                            (OUT_DIR / f"pdf_{len(downloads_seen)}.pdf").write_bytes(body_bytes)
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
                    "url": req.url,         # URL COMPLETA con query params
                    "post": req.post_data,
                    "headers": dict(req.headers),
                })

        page.on("response", on_response)
        page.on("request",  on_request)
        page.on("download", lambda dl: downloads_seen.append(dl.url) or print(f"  *** DOWNLOAD: {dl.url}"))

        # ── Login ────────────────────────────────────────────────────────────
        print("[1] Cargando app...")
        await page.goto(URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3000)
        print(f"  URL: {page.url}")

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
        await page.screenshot(path=str(OUT_DIR / "01_home.png"))

        # ── Navegar al histórico ──────────────────────────────────────────────
        print("\n[2] Navegando a Actividad → Histórico por tipo de Operación")

        actividad_el = page.get_by_text("Actividad", exact=True).first
        if await actividad_el.count() > 0:
            await actividad_el.click()
            await page.wait_for_timeout(2000)

        hist_el = page.get_by_text("Histórico por tipo de Operación", exact=False).first
        if await hist_el.count() > 0:
            print("  Encontrado: 'Histórico por tipo de Operación'")
            await hist_el.click()
            await page.wait_for_timeout(2000)
        else:
            # Buscar "Histórico" genérico
            for txt in ["Histórico", "por tipo", "tipo de Operaci"]:
                el = page.get_by_text(txt, exact=False).first
                if await el.count() > 0:
                    print(f"  Click en: {txt!r}")
                    await el.click()
                    await page.wait_for_timeout(2000)
                    break

        await page.screenshot(path=str(OUT_DIR / "02_historico.png"))
        url_hist = page.url
        text = await page.evaluate("() => document.body.innerText.slice(0,1500)")
        (OUT_DIR / "02_historico_text.txt").write_text(text)
        print(f"  URL: {url_hist}")
        print(f"  Texto visible:\n{text[:600]}")

        # ── Explorar selects disponibles ──────────────────────────────────────
        print("\n[3] Analizando controles de la página")
        controls = await page.evaluate("""() => {
            const results = [];
            // Selects nativos
            document.querySelectorAll('select').forEach(el => {
                const opts = Array.from(el.options).map(o => ({value: o.value, text: o.text}));
                results.push({type:'select', id:el.id, name:el.name, options:opts, value:el.value});
            });
            // MUI Select (combobox con input hidden)
            document.querySelectorAll('[role="button"][aria-haspopup="listbox"], [role="combobox"]').forEach(el => {
                results.push({type:'mui-select', text:el.innerText?.trim()?.slice(0,60), classes:el.className?.slice(0,60)});
            });
            // Inputs de texto/fecha
            document.querySelectorAll('input:not([type="hidden"])').forEach(el => {
                if (el.offsetWidth > 0)
                    results.push({type:'input', inputType:el.type, id:el.id, name:el.name, placeholder:el.placeholder, value:el.value?.slice(0,30)});
            });
            return results;
        }""")
        print(f"  Controls ({len(controls)}):")
        for c in controls:
            print(f"    {c}")
        (OUT_DIR / "03_controls.json").write_text(json.dumps(controls, indent=2, ensure_ascii=False))

        # ── Seleccionar Cauciones ─────────────────────────────────────────────
        print("\n[4] Seleccionando Cauciones")

        # Intentar con select nativo primero
        native_selects = [c for c in controls if c.get('type') == 'select']
        if native_selects:
            for sel_info in native_selects:
                opts = [o['text'] for o in sel_info.get('options', [])]
                print(f"  Select options: {opts}")
                if any('Cauci' in o for o in opts):
                    sel = page.locator(f"select#{sel_info['id']}" if sel_info.get('id') else "select").first
                    await sel.select_option(label=[o['text'] for o in sel_info['options'] if 'Cauci' in o['text']][0])
                    await page.wait_for_timeout(2000)
                    break

        # Si no, usar el combo MUI
        caucion_el = page.get_by_text("Cauciones", exact=True).first
        if await caucion_el.count() > 0:
            print("  Click en 'Cauciones' directo")
            await caucion_el.click()
            await page.wait_for_timeout(2000)
        else:
            # Abrir el combo primero
            combo = page.locator("[role='button'][aria-haspopup='listbox'], [role='combobox']").first
            if await combo.count() > 0:
                await combo.click()
                await page.wait_for_timeout(1000)
                caucion_opt = page.get_by_text("Cauciones", exact=True).first
                if await caucion_opt.count() > 0:
                    await caucion_opt.click()
                    await page.wait_for_timeout(2000)

        await page.screenshot(path=str(OUT_DIR / "04_caucion.png"))
        text = await page.evaluate("() => document.body.innerText.slice(0,1000)")
        print(f"  Post-selección: {text[:300]}")

        # ── Setear fecha ──────────────────────────────────────────────────────
        print(f"\n[5] Seteando fecha: {TEST_DATE}")

        # Capturar inputs después de seleccionar Cauciones
        inputs_now = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input:not([type="hidden"])')).filter(el => el.offsetWidth > 0).map(el => ({
                type:el.type, id:el.id, name:el.name, placeholder:el.placeholder, value:el.value?.slice(0,30)
            }));
        }""")
        print(f"  Inputs visibles: {inputs_now}")

        # Intentar setear fecha en inputs de fecha
        for inp in inputs_now:
            if inp.get('type') == 'date' or 'fecha' in inp.get('placeholder','').lower() or 'date' in inp.get('placeholder','').lower():
                sel = f"input#{inp['id']}" if inp.get('id') else "input[type='date']"
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(TEST_DATE)
                    print(f"  Seteado {TEST_DATE} en {sel}")

        # Click ARS o botón de búsqueda
        for btn_text in ["ARS", "Buscar", "Consultar", "Ver", "Filtrar"]:
            btn = page.get_by_role("button", name=btn_text).first
            if await btn.count() == 0:
                btn = page.get_by_text(btn_text, exact=True).first
            if await btn.count() > 0:
                print(f"  Click en '{btn_text}'")
                await btn.click()
                await page.wait_for_timeout(3000)
                break

        await page.screenshot(path=str(OUT_DIR / "05_results.png"))
        text = await page.evaluate("() => document.body.innerText.slice(0,2000)")
        (OUT_DIR / "05_results_text.txt").write_text(text)
        print(f"  Resultados:\n{text[:400]}")

        # ── Click en primer boleto ────────────────────────────────────────────
        print("\n[6] Clickeando primer boleto para capturar endpoint de descarga")
        await page.wait_for_timeout(2000)

        # Buscar filas de tabla o items clickeables
        row_selectors = [
            "tr:nth-child(2)",
            "tbody tr:first-child",
            "[role='row']:nth-child(2)",
            ".operation-item",
            ".list-item",
            "li:first-child",
        ]
        clicked = False
        for sel in row_selectors:
            el = page.locator(sel).first
            if await el.count() > 0:
                try:
                    text_row = await el.inner_text()
                    if text_row.strip():
                        print(f"  Click en {sel}: {text_row[:60]!r}")
                        await el.click()
                        await page.wait_for_timeout(3000)
                        clicked = True
                        await page.screenshot(path=str(OUT_DIR / "06_boleto_detalle.png"))
                        break
                except Exception:
                    pass

        # También buscar botones de descarga / PDF
        for btn_text in ["Descargar", "PDF", "Comprobante", "Ver comprobante", "Boleto"]:
            btn = page.get_by_text(btn_text, exact=False).first
            if await btn.count() > 0:
                print(f"  *** Encontrado botón: {btn_text!r}")
                await btn.click()
                await page.wait_for_timeout(3000)
                await page.screenshot(path=str(OUT_DIR / f"07_download_{btn_text.lower()}.png"))

        # ── Exploración manual ────────────────────────────────────────────────
        print("""
========================================================
  EXPLORACIÓN MANUAL (90s)

  Por favor:
  1. Si no se seleccionó Cauciones automáticamente, hacelo manual
  2. Hacé click en un boleto individual → capturar endpoint de PDF
  3. También probá "Pases" en el combo
========================================================
        """)
        await page.wait_for_timeout(90_000)

        # ── Resumen COMPLETO con query params ─────────────────────────────────
        print(f"\n[7] TODOS LOS API CALLS CON URL COMPLETA ({len(requests_log)}):")
        seen = set()
        for r in requests_log:
            url = r['url']
            key = r['method'] + " " + url.replace(API_BASE, "")
            if key not in seen:
                seen.add(key)
                path = url.replace(API_BASE, "")
                print(f"  {r['method']} {path[:120]}")
                if r.get('post'):
                    print(f"    POST: {r['post'][:300]}")

        print(f"\n[8] RESPONSES ({len(responses_captured)}):")
        for url, info in responses_captured.items():
            path = info['path']
            qs = info.get('query', '')
            try:
                d = json.loads(info['body'])
                if isinstance(d, list):
                    s = f"lista {len(d)}, keys={list(d[0].keys())[:5] if d else []}"
                elif isinstance(d, dict):
                    s = f"keys={list(d.keys())[:6]}"
                    if 'content' in d and d['content']:
                        s += f", content[0]={list(d['content'][0].keys())[:5]}"
                else:
                    s = "?"
            except Exception:
                s = info['body'][:60]
            print(f"  {info['status']} {path[:70]}")
            if qs:
                print(f"    ?{qs[:120]}")
            print(f"    → {s}")

        if downloads_seen:
            print(f"\n  *** PDFs/DOWNLOADS detectados:")
            for d in downloads_seen:
                print(f"    {d}")

        (OUT_DIR / "responses.json").write_text(json.dumps(responses_captured, indent=2, ensure_ascii=False))
        (OUT_DIR / "requests.json").write_text(json.dumps(requests_log, indent=2, ensure_ascii=False))
        print(f"\n  Guardado en {OUT_DIR}/")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
diag_dhalmore5.py — Descubre estructura de navegación y endpoints de operaciones.

Navega por todas las sub-secciones de Actividad para encontrar boletos/operaciones.

Uso:
    python3 diag_dhalmore5.py
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
OUT_DIR     = Path("downloads/diag_dhalmore5")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = Path("browser_profiles/dhalmore")

from playwright.async_api import async_playwright


async def main():
    bearer = ""
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
                    if resp.status == 200 and len(body_text) > 5:
                        responses_captured[resp.url] = {
                            "status": resp.status,
                            "path": path,
                            "body": body_text[:8000],
                        }
                        if body_text.startswith(("[", "{")):
                            short = path.strip("/").replace("/", "_")[:60].replace("%", "").replace("|", "")
                            try:
                                parsed = json.loads(body_text)
                                (OUT_DIR / f"{short}.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
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

        code_input = page.locator("input[placeholder*='código' i], input[placeholder*='code' i]").first
        if await code_input.count() > 0:
            print("  ⚠️  Device verification!")
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

        confirm = page.locator("button:has-text('Continuar')").first
        if await confirm.count() > 0:
            await confirm.click()
            await page.wait_for_timeout(3000)

        await page.wait_for_timeout(6000)
        print(f"  URL: {page.url}, Bearer: {'OK' if bearer else 'MISSING'}")

        if not bearer:
            print("  ERROR: Sin bearer")
            await context.close()
            return

        # ── Descubrir navegación ──────────────────────────────────────────────
        print(f"\n[2] Descubriendo estructura de navegación")
        await page.screenshot(path=str(OUT_DIR / "01_home.png"))

        # Dumpear todos los links del nav
        nav_info = await page.evaluate("""() => {
            const links = [];
            // Links en nav/header
            document.querySelectorAll('nav a, header a, .menu a, .navbar a, [role="navigation"] a').forEach(a => {
                links.push({ text: a.innerText.trim(), href: a.href, classes: a.className });
            });
            // También botones del nav
            document.querySelectorAll('nav button, header button, .menu button').forEach(b => {
                links.push({ text: b.innerText.trim(), href: null, classes: b.className });
            });
            // Sidebar items
            document.querySelectorAll('.sidebar a, .side-nav a, aside a').forEach(a => {
                links.push({ text: a.innerText.trim(), href: a.href, classes: a.className });
            });
            return links.filter(l => l.text.length > 0 && l.text.length < 50);
        }""")
        print(f"  Nav links encontrados: {len(nav_info)}")
        for item in nav_info:
            print(f"    [{item['text']!r}] href={item.get('href','')}")
        (OUT_DIR / "nav_links.json").write_text(json.dumps(nav_info, indent=2, ensure_ascii=False))

        # Dumpear HTML completo del nav
        nav_html = await page.evaluate("""() => {
            const nav = document.querySelector('nav') || document.querySelector('header') || document.body;
            return nav.innerHTML.slice(0, 5000);
        }""")
        (OUT_DIR / "nav_html.txt").write_text(nav_html)

        # ── Navegar a Actividad y explorar sub-menús ──────────────────────────
        print(f"\n[3] Navegando a Actividad y explorando sub-secciones")

        # Primero hacer hover sobre Actividad para ver el submenu
        actividad_el = page.get_by_text("Actividad", exact=True).first
        if await actividad_el.count() > 0:
            print("  Hover sobre Actividad")
            await actividad_el.hover()
            await page.wait_for_timeout(1000)
            await page.screenshot(path=str(OUT_DIR / "02_actividad_hover.png"))

            # Capturar submenu si aparece
            submenu = await page.evaluate("""() => {
                const menus = document.querySelectorAll('[class*="dropdown"], [class*="submenu"], [class*="sub-menu"], ul li ul, .menu-open');
                const items = [];
                menus.forEach(m => {
                    m.querySelectorAll('a, button').forEach(el => {
                        items.push({ text: el.innerText.trim(), href: el.href || null });
                    });
                });
                return items.filter(i => i.text.length > 0 && i.text.length < 50);
            }""")
            print(f"  Submenu items: {submenu}")

        # Click Actividad
        if await actividad_el.count() > 0:
            await actividad_el.click()
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(OUT_DIR / "03_actividad.png"))
            print(f"  URL: {page.url}")

            # Ver qué sub-items aparecen ahora
            sidebar_html = await page.evaluate("""() => {
                // Buscar sidebar/submenu que apareció
                const sel = '.sidebar, .side-menu, aside, [class*="menu"][class*="open"], [class*="submenu"], [class*="sub-nav"]';
                const el = document.querySelector(sel);
                return el ? el.innerHTML.slice(0, 3000) : document.querySelector('nav').innerHTML.slice(0, 3000);
            }""")
            (OUT_DIR / "sidebar_after_actividad.txt").write_text(sidebar_html)

            # Buscar todos los links visibles ahora
            visible_links = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a[href], button'))
                    .filter(el => el.offsetWidth > 0 && el.offsetHeight > 0)
                    .map(el => ({ text: el.innerText.trim().slice(0, 40), href: el.href || null }))
                    .filter(l => l.text.length > 1);
            }""")
            print(f"  Links visibles ({len(visible_links)}):")
            for link in visible_links:
                if link['text'] and link['text'] not in ['', '\n']:
                    print(f"    [{link['text']!r}] {link.get('href','')[:60]}")

        # ── Buscar y navegar secciones de operaciones ─────────────────────────
        print(f"\n[4] Buscando secciones de operaciones/boletos")

        targets = [
            "Mis operaciones", "Operaciones", "Boletos", "Mis boletos",
            "Cauciones", "Pases", "Comprobantes", "Histórico de operaciones",
            "Mis movimientos", "Movimientos", "Estado de cuenta",
        ]
        for text in targets:
            el = page.get_by_text(text, exact=False).first
            if await el.count() > 0:
                print(f"\n  *** Encontrado: '{text}'")
                await el.click()
                await page.wait_for_timeout(4000)
                await page.screenshot(path=str(OUT_DIR / f"section_{text[:20].lower().replace(' ','_')}.png"))
                print(f"  URL: {page.url}")
                page_text = await page.evaluate("() => document.body.innerText.slice(0, 500)")
                print(f"  Texto: {page_text[:200]}")

        # ── Intentar rutas directas ───────────────────────────────────────────
        print(f"\n[5] Probando rutas SPA directas")
        spa_routes = [
            "/actividad/operaciones",
            "/actividad/boletos",
            "/actividad/comprobantes",
            "/actividad/movimientos",
            "/actividad/historial",
            "/actividad/mis-operaciones",
            "/actividad/cauciones",
            "/operaciones",
            "/boletos",
            "/movimientos",
            "/estado-cuenta",
            "/historial",
        ]
        for route in spa_routes:
            try:
                url = URL_BASE.rstrip("/") + route
                await page.goto(url, wait_until="networkidle", timeout=15_000)
                await page.wait_for_timeout(2000)
                current = page.url
                if "desktop" not in current and "auth0" not in current:
                    text = await page.evaluate("() => document.body.innerText.slice(0, 200)")
                    print(f"  {route} → {current.split('/')[-1]}: {text[:80]}")
                    await page.screenshot(path=str(OUT_DIR / f"route_{route.replace('/','_')}.png"))
            except Exception as e:
                print(f"  {route} → ERROR: {e}")

        # ── Volver a home y espera manual ─────────────────────────────────────
        print(f"\n[6] Volviendo a home y exploración manual (60s)")
        await page.goto(URL_BASE, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(3000)

        # Intentar hover/click en todos los items de nav para capturar el menú completo
        print("  Capturando estructura completa de la página...")
        full_text = await page.evaluate("() => document.body.innerText")
        (OUT_DIR / "home_full_text.txt").write_text(full_text[:10000])

        await page.wait_for_timeout(60_000)

        # ── Resumen ───────────────────────────────────────────────────────────
        print(f"\n[7] Responses únicas ({len(responses_captured)}):")
        for url, info in responses_captured.items():
            path = url.replace(API_BASE, "").split("?")[0]
            try:
                d = json.loads(info["body"])
                if isinstance(d, list):
                    summary = f"lista {len(d)}"
                elif isinstance(d, dict):
                    summary = f"keys={list(d.keys())[:5]}"
                else:
                    summary = "?"
            except Exception:
                summary = info["body"][:50]
            print(f"  {info['status']} {path[:80]} → {summary}")

        print(f"\n[8] Requests únicas ({len(set(r['url'].split('?')[0] for r in requests_log))}):")
        seen = set()
        for r in requests_log:
            k = r["method"] + " " + r["url"].replace(API_BASE, "").split("?")[0]
            if k not in seen:
                seen.add(k)
                print(f"  {k}")

        (OUT_DIR / "responses.json").write_text(json.dumps(responses_captured, indent=2, ensure_ascii=False))
        (OUT_DIR / "requests.json").write_text(json.dumps(requests_log, indent=2, ensure_ascii=False))
        print(f"\n  Guardado en {OUT_DIR}/")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

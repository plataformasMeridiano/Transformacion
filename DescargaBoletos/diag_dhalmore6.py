"""
diag_dhalmore6.py — Explora Resultados y Cuenta para encontrar boletos/comprobantes.

Objetivo: encontrar la sección de operaciones ejecutadas (cauciones/pases).

Uso:
    python3 diag_dhalmore6.py
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
OUT_DIR     = Path("downloads/diag_dhalmore6")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = Path("browser_profiles/dhalmore")

from playwright.async_api import async_playwright


async def capture_page_state(page, label, out_dir):
    """Captura screenshot y texto de la página actual."""
    await page.screenshot(path=str(out_dir / f"{label}.png"))
    text = await page.evaluate("() => document.body.innerText.slice(0, 2000)")
    (out_dir / f"{label}_text.txt").write_text(text)
    print(f"  [{label}] URL: {page.url}")
    print(f"  texto: {text[:300]}\n")
    return text


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
                except Exception:
                    pass
            if "fermi" in resp.url and "sentry" not in resp.url and "websocket" not in resp.url:
                try:
                    body_bytes = await resp.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    path = resp.url.replace(API_BASE, "").split("?")[0]
                    if resp.status == 200 and len(body_text) > 5:
                        responses_captured[resp.url] = {
                            "status": resp.status, "path": path, "body": body_text[:8000],
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
                requests_log.append({"method": req.method, "url": req.url, "post": req.post_data})

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── Login ────────────────────────────────────────────────────────────
        print("[1] Navegando a app...")
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

        # ── Capturar HTML del layout completo ─────────────────────────────────
        print("\n[2] Capturando estructura completa del DOM")
        await page.screenshot(path=str(OUT_DIR / "01_home.png"))

        # Buscar elementos con texto de navegación
        nav_items = await page.evaluate("""() => {
            const results = [];
            const keywords = ['Inicio','Mercados','Portafolio','Resultados','Actividad','Cuenta'];
            document.querySelectorAll('*').forEach(el => {
                const text = el.innerText?.trim();
                if (text && keywords.includes(text) && el.offsetWidth > 0) {
                    results.push({
                        tag: el.tagName,
                        text: text,
                        classes: el.className,
                        href: el.href || null,
                        id: el.id,
                    });
                }
            });
            return results;
        }""")
        print(f"  Nav items encontrados: {len(nav_items)}")
        for item in nav_items:
            print(f"    <{item['tag']}> [{item['text']}] classes={item['classes'][:60]} href={item.get('href')}")
        (OUT_DIR / "nav_items.json").write_text(json.dumps(nav_items, indent=2))

        # ── Explorar cada sección ─────────────────────────────────────────────
        sections = ["Resultados", "Cuenta", "Portafolio", "Actividad"]

        for section_name in sections:
            print(f"\n[3.{section_name}] Navegando a '{section_name}'")
            el = page.get_by_text(section_name, exact=True).first
            if await el.count() == 0:
                el = page.get_by_role("button", name=section_name).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_timeout(3000)
                await capture_page_state(page, f"section_{section_name.lower()}", OUT_DIR)

                # Buscar sub-items del menú
                sidebar_items = await page.evaluate("""() => {
                    const items = [];
                    document.querySelectorAll('a, button, [role="menuitem"], [role="tab"], [role="listitem"]').forEach(el => {
                        const text = el.innerText?.trim();
                        if (text && text.length > 1 && text.length < 60 && el.offsetWidth > 0 && el.offsetHeight > 0) {
                            items.push({ text, tag: el.tagName, href: el.href || null, classes: el.className?.slice(0, 50) });
                        }
                    });
                    return items;
                }""")
                print(f"  Sidebar items ({len(sidebar_items)}):")
                for item in sidebar_items:
                    print(f"    [{item['text']!r}] {item.get('href','')[:60]}")
                (OUT_DIR / f"sidebar_{section_name.lower()}.json").write_text(
                    json.dumps(sidebar_items, indent=2, ensure_ascii=False))

                # Intentar hacer click en sub-items relevantes
                keywords = ["Comprobante", "Boleto", "Operaci", "Movimiento",
                            "Estado", "Extracto", "Historial", "Resumen"]
                for item in sidebar_items:
                    for kw in keywords:
                        if kw.lower() in item['text'].lower():
                            print(f"  *** Encontrado sub-item: {item['text']!r}")
                            sub_el = page.get_by_text(item['text'], exact=False).first
                            if await sub_el.count() > 0:
                                await sub_el.click()
                                await page.wait_for_timeout(3000)
                                await capture_page_state(
                                    page,
                                    f"sub_{section_name.lower()}_{item['text'][:20].lower().replace(' ','_')}",
                                    OUT_DIR
                                )

        # ── Ver el Portafolio para buscar Cauciones/Pases ─────────────────────
        print("\n[4] Buscando Cauciones/Pases en Portafolio")
        portafolio_el = page.get_by_text("Portafolio", exact=True).first
        if await portafolio_el.count() > 0:
            await portafolio_el.click()
            await page.wait_for_timeout(3000)

            # Buscar items con cauciones/pases
            for text in ["Cauciones", "Caución", "Pases", "Pase", "RCI"]:
                el = page.get_by_text(text, exact=False).first
                if await el.count() > 0:
                    print(f"  *** Encontrado: {text!r}")
                    await el.click()
                    await page.wait_for_timeout(3000)
                    await capture_page_state(page, f"portafolio_{text.lower()}", OUT_DIR)

        # ── Exploración manual extendida ─────────────────────────────────────
        print("\n[5] Exploración manual (120s) — navegá a la sección de boletos/operaciones")
        print("     Los API calls serán capturados automáticamente")
        await page.wait_for_timeout(120_000)

        # ── Resumen ───────────────────────────────────────────────────────────
        print(f"\n[6] API calls únicos capturados:")
        seen = set()
        interesting = []
        for r in requests_log:
            url = r['url']
            path = url.replace(API_BASE, "").split("?")[0]
            if path not in seen:
                seen.add(path)
                line = f"  {r['method']} {path}"
                print(line)
                if any(kw in url.lower() for kw in ["operation", "boleto", "ticket", "statement",
                                                      "movement", "comprobante", "extract"]):
                    interesting.append(line)

        if interesting:
            print("\n  *** ENDPOINTS INTERESANTES:")
            for line in interesting:
                print(line)

        print(f"\n  Responses de la API capturadas ({len(responses_captured)} endpoints):")
        for url, info in responses_captured.items():
            path = url.replace(API_BASE, "").split("?")[0]
            try:
                d = json.loads(info["body"])
                s = f"lista={len(d)}" if isinstance(d, list) else f"keys={list(d.keys())[:5]}"
            except Exception:
                s = info["body"][:50]
            print(f"  {info['status']} {path[:70]} → {s}")

        (OUT_DIR / "responses.json").write_text(json.dumps(responses_captured, indent=2, ensure_ascii=False))
        (OUT_DIR / "requests.json").write_text(json.dumps(requests_log, indent=2, ensure_ascii=False))
        print(f"\n  Guardado en {OUT_DIR}/")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

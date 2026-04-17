"""
Exploración de secciones post-login en Metrocorp — buscar boletos/comprobantes.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL      = "https://be.bancocmf.com.ar/"
DNI      = "13654870"
USUARIO  = "Meri1496"
PASSWORD = "Meridiano25*"

OUT = Path("downloads/metro_explore")
OUT.mkdir(parents=True, exist_ok=True)

api_responses: list[dict] = []
access_token: str = ""


async def log_responses(page):
    async def on_response(resp):
        global access_token
        if resp.request.resource_type in ("xhr", "fetch"):
            entry = {"url": resp.url, "status": resp.status,
                     "method": resp.request.method,
                     "ct": resp.headers.get("content-type", "")}
            try:
                body = await resp.body()
                if b"%PDF" in body[:10]:
                    entry["body_type"] = "PDF"
                    entry["body_len"] = len(body)
                    entry["body"] = body  # guardar raw
                elif "json" in entry["ct"]:
                    j = json.loads(body)
                    entry["body"] = j
                    # Capturar access_token
                    if "access_token" in j:
                        access_token = j["access_token"]
                        print(f"  [TOKEN] access_token capturado: {access_token[:30]}...")
                    elif isinstance(j.get("data"), dict) and "_accessToken" in j["data"]:
                        access_token = j["data"]["_accessToken"]
                        print(f"  [TOKEN] _accessToken capturado: {access_token[:30]}...")
                else:
                    entry["body_preview"] = body[:400].decode("utf-8", errors="replace")
            except Exception as e:
                entry["err"] = str(e)
            api_responses.append(entry)

            b = entry.get("body", {})
            if isinstance(b, dict):
                info = f"keys={list(b.keys())[:4]}"
            elif entry.get("body_type") == "PDF":
                info = f"PDF {entry['body_len']}b"
            else:
                info = entry.get("body_preview", "")[:80]
            print(f"  API [{entry['status']}] {entry['method']} {entry['url'][:90]}")
    page.on("response", on_response)


async def login(page):
    print("[LOGIN] Cargando...")
    await page.goto(URL, wait_until="networkidle", timeout=60_000)
    await page.wait_for_timeout(2000)

    await page.fill("#document\\.number", DNI)
    await page.fill("#login\\.step1\\.username", USUARIO)
    await page.click("button[type='submit']:has-text('Continuar')")
    await page.wait_for_url(lambda u: "loginStep2" in u, timeout=15_000)

    await page.wait_for_timeout(1000)
    await page.fill("#login\\.step2\\.password", PASSWORD)
    await page.click("button[type='submit']:has-text('Ingresar')")
    await page.wait_for_url(lambda u: "desktop" in u, timeout=20_000)
    await page.wait_for_timeout(3000)
    print(f"[LOGIN] Exitoso — {page.url}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=150)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await log_responses(page)

        await login(page)

        # ── Explorar sección "Metrocorp" ────────────────────────────────────
        print("\n[1] Clickeando 'Metrocorp'...")
        await page.locator("nav button, aside button").filter(has_text="Metrocorp").first.click()
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT / "40_metrocorp.png"), full_page=True)
        print(f"    URL: {page.url}")

        # Ver submenu o secciones
        visible_buttons = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button, a[href]'))
                .filter(el => el.offsetParent !== null)
                .map(el => ({
                    tag: el.tagName,
                    text: el.textContent.trim().substring(0, 80),
                    href: el.getAttribute('href') || ''
                })).filter(el => el.text.length > 0 && el.text.length < 80)
        """)
        print(f"    Botones/links visibles ({len(visible_buttons)}):")
        for b in visible_buttons[:30]:
            print(f"      {b['text']!r} href={b['href']!r}")

        # API calls después del click en Metrocorp
        print(f"\n    API calls en Metrocorp: {len(api_responses)} total")
        for r in api_responses[-10:]:
            b = r.get("body", {})
            if isinstance(b, dict):
                data = b.get("data")
                if isinstance(data, dict):
                    info = f"data_keys={list(data.keys())[:6]}"
                elif isinstance(data, list) and data:
                    info = f"data=list({len(data)}) first={str(data[0])[:80]}"
                else:
                    info = str(data)[:100] if data else str(list(b.keys())[:5])
            else:
                info = r.get("body_preview", "")[:80]
            print(f"  [{r['status']}] {r['method']} {r['url'][:90]}")
            print(f"    {info}")

        # ── Explorar submenú Metrocorp (boletos, comprobantes, etc.) ────────
        print("\n[2] Buscando subsección boletos en Metrocorp...")
        for word in ["boleto", "comprobante", "estado", "orden", "operaci", "download", "echeq"]:
            count = await page.locator(f"text=/{word}/i").count()
            if count > 0:
                texts = []
                for i in range(min(5, count)):
                    try:
                        t = await page.locator(f"text=/{word}/i").nth(i).text_content()
                        texts.append(t.strip()[:60])
                    except Exception:
                        pass
                print(f"    '{word}': {texts}")

        await page.screenshot(path=str(OUT / "41_metrocorp_full.png"), full_page=True)

        # ── Explorar "Historial" ────────────────────────────────────────────
        print("\n[3] Clickeando 'Historial'...")
        await page.locator("nav button, aside button, button").filter(has_text="Historial").first.click()
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUT / "42_historial.png"), full_page=True)
        print(f"    URL: {page.url}")

        # APIs del historial
        print(f"\n    API calls recientes:")
        for r in api_responses[-10:]:
            b = r.get("body", {})
            if isinstance(b, dict):
                data = b.get("data")
                if isinstance(data, dict):
                    info = f"data_keys={list(data.keys())[:6]}"
                elif isinstance(data, list):
                    info = f"data=list({len(data)})" + (f" first={str(data[0])[:80]}" if data else "")
                else:
                    info = str(data)[:100] if data else f"keys={list(b.keys())[:5]}"
            else:
                info = r.get("body_preview", "")[:80]
            print(f"  [{r['status']}] {r['method']} {r['url'][:90]}")
            print(f"    {info}")

        # ── Guardar HTML de cada sección ────────────────────────────────────
        html = await page.evaluate("() => document.body.innerHTML")
        with open(OUT / "historial_body.txt", "w") as f:
            f.write(html)

        # ── Intentar navegar a URLs conocidas de inversiones/boletos ───────
        print("\n[4] Probando URLs directas...")
        test_urls = [
            "https://be.bancocmf.com.ar/investments",
            "https://be.bancocmf.com.ar/investments/boletos",
            "https://be.bancocmf.com.ar/history",
            "https://be.bancocmf.com.ar/historial",
            "https://be.bancocmf.com.ar/echeq",
            "https://be.bancocmf.com.ar/metrocorp",
        ]
        for url in test_urls:
            try:
                prev_api_count = len(api_responses)
                await page.goto(url, wait_until="networkidle", timeout=15_000)
                await page.wait_for_timeout(1000)
                new_apis = api_responses[prev_api_count:]
                if new_apis:
                    print(f"  {url} → {page.url}")
                    for r in new_apis[:5]:
                        print(f"    [{r['status']}] {r['url'][:80]}")
                else:
                    print(f"  {url} → redirected to {page.url} (no new APIs)")
            except Exception as e:
                print(f"  {url} → error: {e}")

        # ── Guardar log completo ────────────────────────────────────────────
        safe_responses = []
        for r in api_responses:
            safe = {k: v for k, v in r.items() if k != "body" or not isinstance(v, bytes)}
            if isinstance(safe.get("body"), bytes):
                safe["body"] = f"<binary {len(safe['body'])} bytes>"
            safe_responses.append(safe)

        with open(OUT / "api_log4.json", "w") as f:
            json.dump(safe_responses, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n[5] Logs guardados en {OUT}/")

        print("\nEsperando 10s...")
        await page.wait_for_timeout(10_000)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

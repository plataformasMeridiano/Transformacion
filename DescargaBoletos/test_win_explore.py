"""
Exploración visual del portal WIN — Movimientos → Pesos x tipo de operación.

Uso:
    python3 test_win_explore.py

Hace login, navega al submenú objetivo, vuelca info de filtros y tabla,
y mantiene el browser abierto 90 segundos para inspección visual.
"""

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

import os

CONFIG_PATH = Path(__file__).parent / "config.json"


def get_win_config() -> tuple[dict, dict]:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    general = config["general"]
    alyc = next(a for a in config["alycs"] if a["nombre"] == "WIN")
    return alyc, general


def resolve(value: str) -> str:
    import re
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], value)


def sep(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print('─'*60)


async def main():
    alyc, general = get_win_config()
    url_login = alyc["url_login"]
    documento = resolve(alyc["documento"])
    usuario   = resolve(alyc["usuario"])
    contrasena = resolve(alyc["contrasena"])

    print(f"ALYC    : {alyc['nombre']}")
    print(f"URL     : {url_login}")
    print(f"headless: {general['headless']}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        # ── LOGIN ────────────────────────────────────────────────────────────
        sep("1. LOGIN")
        await page.goto(url_login, wait_until="load", timeout=30_000)
        await page.fill("input[name='Dni']", documento)
        await page.fill("#usuario", usuario)
        await page.fill("#passwd", contrasena)
        await page.click("#loginButton")
        await page.wait_for_url(lambda url: "/Login" not in url, timeout=30_000)
        print(f"  Login OK — URL: {page.url}")
        await asyncio.sleep(2)

        # ── NAVEGAR DIRECTO A "Pesos x tipo de operación" ────────────────────
        sep("2. NAVEGAR a /Consultas/PesosPorTipoOperacion")
        base = "https://clientes.winsa.com.ar"
        await page.goto(f"{base}/Consultas/PesosPorTipoOperacion", wait_until="load", timeout=30_000)
        print(f"  URL: {page.url}")
        await asyncio.sleep(2)
        screenshot = Path(__file__).parent / "win_pesos_tipo.png"
        await page.screenshot(path=str(screenshot), full_page=True)
        print(f"  Screenshot guardado: {screenshot}")

        # ── INSPECCIONAR FILTROS ──────────────────────────────────────────────
        sep("6. INPUTS / SELECTS en la página de filtro")
        form_els = await page.evaluate("""
            () => {
                const result = [];
                for (const el of document.querySelectorAll('input, select, textarea, button')) {
                    result.push({
                        tag:         el.tagName,
                        type:        el.getAttribute('type') || '',
                        id:          el.id || '',
                        name:        el.getAttribute('name') || '',
                        cls:         el.className || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        value:       el.tagName === 'SELECT'
                                        ? [...el.options].map(o => o.text + '=' + o.value).join(' | ')
                                        : (el.value || ''),
                    });
                }
                return result;
            }
        """)
        for el in form_els:
            print(f"  <{el['tag']} type={el['type']}> id={el['id']} name={el['name']} "
                  f"cls={el['cls'][:40]} placeholder={el['placeholder']} value={el['value'][:80]}")

        # ── APLICAR FILTRO DE PRUEBA ─────────────────────────────────────────
        sep("7. APLICAR FILTRO — 25/02/2026 al 26/02/2026, Cauciones")
        fecha_desde = "25/02/2026"
        fecha_hasta = "26/02/2026"
        print(f"  Rango: {fecha_desde} → {fecha_hasta}")

        # Fecha desde
        await page.click("#idInputFechaDesde", click_count=3)
        await page.fill("#idInputFechaDesde", fecha_desde)
        print("  Fecha desde OK")

        # Fecha hasta
        await page.click("#idInputFechaHasta", click_count=3)
        await page.fill("#idInputFechaHasta", fecha_hasta)
        print("  Fecha hasta OK")

        # Combo tipo operación — valor "03" = Cauciones
        await page.select_option("#idInputTipoCombo1", value="03")
        selected = await page.eval_on_selector("#idInputTipoCombo1",
                                               "el => el.options[el.selectedIndex].text")
        print(f"  Combo tipo → '{selected}' OK")

        # Botón Consultar
        await page.click("button.boton-consulta")
        print("  Botón Consultar OK")

        await page.wait_for_load_state("networkidle", timeout=15_000)
        await asyncio.sleep(2)

        screenshot2 = Path(__file__).parent / "win_resultados.png"
        await page.screenshot(path=str(screenshot2), full_page=True)
        print(f"  Screenshot resultados: {screenshot2}")

        # ── INSPECCIONAR TABLA DE RESULTADOS ─────────────────────────────────
        sep("8. TABLA DE RESULTADOS — filas y links")
        tabla_info = await page.evaluate("""
            () => {
                const result = { headers: [], rows: [], links: [], buttons: [] };

                // Headers
                for (const th of document.querySelectorAll('table th, thead td'))
                    result.headers.push(th.innerText.trim());

                // Primeras 5 filas
                let rowCount = 0;
                for (const tr of document.querySelectorAll('table tbody tr')) {
                    if (rowCount++ >= 5) break;
                    const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                    const links = [...tr.querySelectorAll('a')].map(a => ({
                        text: a.innerText.trim(),
                        href: a.getAttribute('href') || '',
                        onclick: a.getAttribute('onclick') || '',
                    }));
                    result.rows.push({ cells, links });
                }

                // Todos los links de la página que puedan ser PDF
                for (const a of document.querySelectorAll('a')) {
                    const href = a.getAttribute('href') || '';
                    const onclick = a.getAttribute('onclick') || '';
                    const text = a.innerText.trim();
                    if (href.toLowerCase().includes('pdf') ||
                        href.toLowerCase().includes('boleto') ||
                        onclick.toLowerCase().includes('pdf') ||
                        onclick.toLowerCase().includes('boleto') ||
                        text.toLowerCase().includes('pdf') ||
                        text.toLowerCase().includes('boleto'))
                        result.links.push({ text, href, onclick });
                }

                // Botones de la página
                for (const btn of document.querySelectorAll('button, input[type=button], input[type=submit]')) {
                    result.buttons.push({
                        text:    btn.innerText?.trim() || btn.getAttribute('value') || '',
                        id:      btn.id || '',
                        onclick: btn.getAttribute('onclick') || '',
                        cls:     btn.className || '',
                    });
                }

                return result;
            }
        """)

        print("  HEADERS:", tabla_info["headers"])
        for i, row in enumerate(tabla_info["rows"]):
            print(f"  FILA {i}: {row['cells']}")
            for lnk in row["links"]:
                print(f"    LINK: [{lnk['text']}] href={lnk['href']} onclick={lnk['onclick']}")

        print("\n  LINKS con 'pdf'/'boleto' en la página:")
        for lnk in tabla_info["links"]:
            print(f"    [{lnk['text']}] href={lnk['href']} onclick={lnk['onclick']}")

        print("\n  BOTONES en la página:")
        for btn in tabla_info["buttons"]:
            print(f"    [{btn['text']}] id={btn['id']} onclick={btn['onclick']} cls={btn['cls'][:50]}")

        # ── CLICK EN "VER" DE LA PRIMERA FILA ───────────────────────────────
        sep("8b. CLICK en primer 'Ver' — inspeccionar getComprobante()")

        # Interceptar requests para ver qué URL se llama
        requests_capturados = []
        page.on("request", lambda req: requests_capturados.append({
            "url": req.url, "method": req.method
        }))

        # Escuchar posibles descargas
        download_ocurrido = []

        async def on_download(dl):
            download_ocurrido.append(dl)

        page.on("download", on_download)

        # Interceptar el POST a GetComprobante para ver request y response completos
        get_comprobante_data = {}

        async def intercept_get_comprobante(route, request):
            if "GetComprobante" in request.url:
                body = request.post_data or ""
                print(f"\n  [INTERCEPT] POST {request.url}")
                print(f"  [INTERCEPT] Body: {body}")
                resp = await route.fetch()
                resp_body = await resp.body()
                get_comprobante_data["status"]  = resp.status
                get_comprobante_data["headers"] = dict(resp.headers)
                get_comprobante_data["body"]    = resp_body
                await route.fulfill(response=resp)
            else:
                await route.continue_()

        await page.route("**/*", intercept_get_comprobante)

        # Click en el primer link "Ver", esperando nueva pestaña en el contexto
        primer_ver = page.locator("table tbody tr a").first
        ver_href = await primer_ver.get_attribute("href")
        print(f"  Clickeando: {ver_href}")

        try:
            async with context.expect_page(timeout=10_000) as new_page_info:
                await primer_ver.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
            print(f"  Nueva pestaña — URL: {new_page.url}")
        except Exception as e:
            print(f"  Sin nueva pestaña: {e}")

        await asyncio.sleep(2)

        # Mostrar resultado de GetComprobante
        if get_comprobante_data:
            print(f"\n  GetComprobante status: {get_comprobante_data['status']}")
            print(f"  Content-Type: {get_comprobante_data['headers'].get('content-type', '?')}")
            body = get_comprobante_data["body"]
            print(f"  Body size: {len(body)} bytes")
            # Si es JSON/texto, mostrar primeros 500 chars
            try:
                body_str = body.decode("utf-8", errors="replace")
                print(f"  Body (primeros 500): {body_str[:500]}")
                # Si parece una URL, la guardamos
                if body_str.strip().startswith("http") or body_str.strip().startswith("/"):
                    print(f"  → Parece una URL: {body_str.strip()}")
            except Exception:
                pass
            # Si es PDF, guardar
            if body[:4] == b"%PDF":
                pdf_path = Path(__file__).parent / "win_comprobante_test.pdf"
                pdf_path.write_bytes(body)
                print(f"  → PDF guardado: {pdf_path} ({len(body)} bytes)")
        else:
            print("  GetComprobante no fue interceptado")

        await asyncio.sleep(1)
        print(f"\n  Requests capturados tras click:")
        for r in requests_capturados[-10:]:
            print(f"    [{r['method']}] {r['url']}")

        print(f"\n  Descargas ocurridas: {len(download_ocurrido)}")
        for dl in download_ocurrido:
            print(f"    {dl.suggested_filename}")

        # ── MANTENER BROWSER ABIERTO ─────────────────────────────────────────
        sep("9. Browser abierto — 90 segundos para inspección visual")
        print("  Podés navegar y explorar. Cerrando en 90s...")
        await asyncio.sleep(90)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

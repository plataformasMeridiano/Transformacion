"""
Exploración visual del portal Puente — post-login, sección de boletos.

Uso:
    python3 test_puente_explore.py

Hace login, vuelca toda la navegación disponible, navega a Movimientos
y mantiene el browser abierto 90 segundos para inspección visual.
"""

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

import os, re

CONFIG_PATH = Path(__file__).parent / "config.json"


def get_puente_config() -> tuple[dict, dict]:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    general = config["general"]
    alyc = next(a for a in config["alycs"] if a["nombre"] == "Puente")
    return alyc, general


def resolve(value: str) -> str:
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], value)


def sep(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print('─'*60)


async def dump_links(page, label: str) -> None:
    sep(label)
    items = await page.evaluate("""
        () => {
            const seen = new Set();
            const result = [];
            for (const el of document.querySelectorAll('a, button, li')) {
                const text = el.innerText?.trim();
                if (!text || text.length > 100) continue;
                const href = el.getAttribute('href') || '';
                const key = text + href;
                if (seen.has(key)) continue;
                seen.add(key);
                const rect = el.getBoundingClientRect();
                result.push({
                    tag:  el.tagName,
                    text: text,
                    href: href,
                    vis:  rect.width > 0 && rect.height > 0,
                });
            }
            return result;
        }
    """)
    for it in items:
        vis = "" if it["vis"] else " [oculto]"
        print(f"  <{it['tag']}> [{it['text']}]  href={it['href']}{vis}")


async def main():
    alyc, general = get_puente_config()
    url_login  = alyc["url_login"]
    documento  = resolve(alyc["documento"])
    usuario    = resolve(alyc["usuario"])
    contrasena = resolve(alyc["contrasena"])

    print(f"ALYC    : {alyc['nombre']}")
    print(f"URL     : {url_login}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        # ── LOGIN ────────────────────────────────────────────────────────────
        sep("1. LOGIN")
        await page.goto(url_login, wait_until="domcontentloaded", timeout=60_000)

        await page.locator("#loginForm input[placeholder='Nro. documento']").fill(documento)
        await page.locator("#loginForm #input_username").fill(usuario)
        await page.locator("#loginForm #input_password").fill(contrasena)
        await page.locator("#loginForm").get_by_text("Ingresar", exact=True).click()
        await page.wait_for_url(lambda url: "/login" not in url, timeout=30_000)
        print(f"  Login OK — URL: {page.url}")
        await asyncio.sleep(2)

        # Screenshot post-login
        shot = Path(__file__).parent / "puente_postlogin.png"
        await page.screenshot(path=str(shot), full_page=True)
        print(f"  Screenshot: {shot}")

        # ── TODOS LOS LINKS POST-LOGIN ────────────────────────────────────────
        await dump_links(page, "2. TODOS LOS LINKS POST-LOGIN")

        # ── NAVEGAR A MOVIMIENTOS ─────────────────────────────────────────────
        sep("3. NAVEGAR A /cuentas/mi-cuenta/movimientos")
        base = "https://www.puentenet.com"
        await page.goto(f"{base}/cuentas/mi-cuenta/movimientos",
                        wait_until="load", timeout=30_000)
        print(f"  URL: {page.url}")
        await asyncio.sleep(2)

        shot2 = Path(__file__).parent / "puente_movimientos.png"
        await page.screenshot(path=str(shot2), full_page=True)
        print(f"  Screenshot: {shot2}")

        await dump_links(page, "4. LINKS EN MOVIMIENTOS")

        # ── INSPECCIONAR FILTROS EN MOVIMIENTOS ──────────────────────────────
        sep("5. INPUTS / SELECTS en Movimientos")
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
                  f"placeholder={el['placeholder']} value={el['value'][:100]}")

        # ── BUSCAR LINKS RELACIONADOS CON BOLETOS ────────────────────────────
        sep("6. LINKS con 'boleto', 'comprobante', 'liquidacion', 'pdf' en toda la página")
        keywords = ["boleto", "comprobante", "liquidacion", "pdf", "descarg"]
        all_links = await page.evaluate("""
            () => [...document.querySelectorAll('a')].map(a => ({
                text:    a.innerText.trim(),
                href:    a.getAttribute('href') || '',
                onclick: a.getAttribute('onclick') || '',
            }))
        """)
        for lnk in all_links:
            combined = (lnk["text"] + lnk["href"] + lnk["onclick"]).lower()
            if any(k in combined for k in ["boleto", "comprobante", "liquidacion", "pdf", "descarg"]):
                print(f"  [{lnk['text']}] href={lnk['href']} onclick={lnk['onclick']}")

        # ── INSPECCIONAR TABLA ────────────────────────────────────────────────
        sep("7. TABLA en Movimientos (primeras 5 filas)")
        tabla = await page.evaluate("""
            () => {
                const result = { headers: [], rows: [] };
                for (const th of document.querySelectorAll('table th, thead td'))
                    result.headers.push(th.innerText.trim());
                let n = 0;
                for (const tr of document.querySelectorAll('table tbody tr')) {
                    if (n++ >= 5) break;
                    const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                    const links = [...tr.querySelectorAll('a')].map(a => ({
                        text: a.innerText.trim(),
                        href: a.getAttribute('href') || '',
                        onclick: a.getAttribute('onclick') || '',
                    }));
                    result.rows.push({ cells, links });
                }
                return result;
            }
        """)
        print(f"  HEADERS: {tabla['headers']}")
        for i, row in enumerate(tabla["rows"]):
            print(f"  FILA {i}: {row['cells']}")
            for lnk in row["links"]:
                print(f"    LINK: [{lnk['text']}] href={lnk['href']} onclick={lnk['onclick']}")

        # ── APLICAR FILTRO Y VER RESULTADOS ──────────────────────────────────
        sep("8. APLICAR FILTRO — 25/02/2026 al 26/02/2026")

        # Opciones del select descripcionFiltro (truncado arriba, volcar completo)
        opciones = await page.evaluate("""
            () => [...document.querySelectorAll('#descripcionFiltro option')]
                    .map(o => o.text + '=' + o.value)
        """)
        print("  Opciones descripcionFiltro:")
        for op in opciones:
            print(f"    {op}")

        # Setear fechas
        await page.fill("#fechaDesde", "25/02/2026")
        await page.fill("#fechaHasta", "26/02/2026")
        print("  Fechas seteadas")

        # Click en "Ver movimientos"
        await page.click("#traerMovimientos")
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await asyncio.sleep(2)

        shot3 = Path(__file__).parent / "puente_resultados.png"
        await page.screenshot(path=str(shot3), full_page=True)
        print(f"  Screenshot resultados: {shot3}")

        # Inspeccionar resultado
        sep("8b. TABLA DE RESULTADOS")
        tabla2 = await page.evaluate("""
            () => {
                const result = { headers: [], rows: [], links: [] };
                for (const th of document.querySelectorAll('table th, thead td'))
                    result.headers.push(th.innerText.trim());
                let n = 0;
                for (const tr of document.querySelectorAll('table tbody tr')) {
                    if (n++ >= 10) break;
                    const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                    if (!cells.length) continue;
                    const links = [...tr.querySelectorAll('a')].map(a => ({
                        text:    a.innerText.trim(),
                        href:    a.getAttribute('href') || '',
                        onclick: a.getAttribute('onclick') || '',
                    }));
                    result.rows.push({ cells, links });
                }
                // Todos los links de la página con palabras clave
                for (const a of document.querySelectorAll('a')) {
                    const href    = a.getAttribute('href') || '';
                    const onclick = a.getAttribute('onclick') || '';
                    const text    = a.innerText.trim();
                    const combined = (text + href + onclick).toLowerCase();
                    if (['boleto','comprobante','pdf','liquidacion','descarg'].some(k => combined.includes(k)))
                        result.links.push({ text, href, onclick });
                }
                return result;
            }
        """)
        print(f"  HEADERS: {tabla2['headers']}")
        for i, row in enumerate(tabla2["rows"]):
            print(f"  FILA {i}: {row['cells'][:6]}")
            for lnk in row["links"]:
                print(f"    LINK: [{lnk['text']}] href={lnk['href']} onclick={lnk['onclick']}")
        print("\n  LINKS con boleto/pdf/comprobante:")
        for lnk in tabla2["links"]:
            print(f"    [{lnk['text']}] href={lnk['href']} onclick={lnk['onclick']}")

        # ── MANTENER BROWSER ABIERTO ─────────────────────────────────────────
        sep("9. Browser abierto — 90 segundos para inspección visual")
        print("  Podés navegar y explorar. Cerrando en 90s...")
        await asyncio.sleep(90)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

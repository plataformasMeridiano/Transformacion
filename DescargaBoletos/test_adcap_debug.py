"""
Debug ADCAP — verifica qué pasa con el datepicker y la grilla para 02/03/2026.
Toma screenshots en cada paso para diagnosticar el problema.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

with open("config.json") as f:
    cfg = json.load(f)

import re
def resolve(v):
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], v)

alyc = next(a for a in cfg["alycs"] if a["nombre"] == "ADCAP")
OUT  = Path("downloads/adcap_debug")
OUT.mkdir(parents=True, exist_ok=True)

FECHA = "02/03/2026"
URL   = alyc["url_login"]


async def fill_datepicker(page, locator, value: str):
    """Intenta distintas estrategias para setear un datepicker de Angular Material."""
    await locator.click()
    await asyncio.sleep(0.3)
    # Seleccionar todo y borrar primero
    await locator.press("Control+a")
    await asyncio.sleep(0.1)
    await locator.press("Delete")
    await asyncio.sleep(0.1)
    # Tipear caracter a caracter para disparar eventos de Angular
    await locator.type(value, delay=80)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Tab")
    await asyncio.sleep(0.5)
    # Leer el valor que quedó
    val = await locator.input_value()
    return val


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=80)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # ── 1. Login ──────────────────────────────────────────────────────
        print(f"[1] Navegando a {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("#input_0", timeout=15_000)

        await page.fill("#input_0", resolve(alyc["usuario"]))
        await page.fill("#input_1", resolve(alyc["contrasena"]))
        await page.click("#btnIngresar")
        await page.wait_for_url(lambda u: "#!/login" not in u, timeout=30_000)
        print(f"[LOGIN] OK — {page.url}")
        await asyncio.sleep(5)

        # ── 2. Navegar a BOLETOS ──────────────────────────────────────────
        print("[2] Clickeando BOLETOS...")
        await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('md-item-content[ng-click]'))
                    if (el.innerText.trim() === 'BOLETOS') { el.click(); break; }
            }
        """)
        await asyncio.sleep(4)
        await page.screenshot(path=str(OUT / "01_boletos.png"), full_page=True)
        print(f"    URL: {page.url}")

        # Ver filas ANTES de filtrar — dump completo
        rows_before = await page.evaluate("""
            () => {
                const rows = [];
                for (const row of document.querySelectorAll('table tr[data-id]')) {
                    const tds = [...row.querySelectorAll('td')].map(td => td.innerText.trim());
                    rows.push({ dataId: row.getAttribute('data-id'), cells: tds });
                }
                return rows;
            }
        """)
        print(f"    Filas antes de filtrar: {len(rows_before)}")
        for r in rows_before:
            print(f"      data-id={r['dataId']} | {r['cells'][:5]}")

        # ── 3. Abrir filtro ────────────────────────────────────────────────
        print("[3] Abriendo filtro...")
        await page.locator("span.icon-filter").first.click()
        await asyncio.sleep(2)
        await page.screenshot(path=str(OUT / "02_filtro_open.png"))

        # Leer las fechas actuales del dialog
        inputs = page.locator(".md-dialog-container input.md-datepicker-input")
        n = await inputs.count()
        print(f"    Inputs de fecha en dialog: {n}")
        for i in range(n):
            val = await inputs.nth(i).input_value()
            print(f"    Input[{i}] valor actual: {val!r}")

        # ── 4. Setear fechas ────────────────────────────────────────────────
        print(f"[4] Seteando fecha {FECHA}...")
        val0 = await fill_datepicker(page, inputs.nth(0), FECHA)
        val1 = await fill_datepicker(page, inputs.nth(1), FECHA)
        print(f"    Input[0] después: {val0!r}")
        print(f"    Input[1] después: {val1!r}")
        await page.screenshot(path=str(OUT / "03_fechas_seteadas.png"))

        # ── 5. Filtrar ──────────────────────────────────────────────────────
        print("[5] Clickeando FILTRAR...")
        await page.locator(".md-dialog-container button", has_text="FILTRAR").click()
        await page.wait_for_load_state("networkidle", timeout=30_000)
        # Esperar que el spinner de Angular desaparezca
        print("    Esperando que desaparezca el spinner...")
        # Esperar que la tabla vuelva a existir (Angular reconstruye el DOM)
        print("    Esperando que la tabla reaparezca...")
        try:
            await page.wait_for_selector("table", timeout=20_000)
            print("    Tabla visible")
        except Exception:
            print("    Timeout esperando tabla")
        await asyncio.sleep(1)
        await page.screenshot(path=str(OUT / "04_filtrado.png"), full_page=True)

        # ── 6. Leer grilla ──────────────────────────────────────────────────
        print("[6] Leyendo grilla...")
        rows = await page.evaluate("""
            () => {
                const result = [];
                for (const row of document.querySelectorAll('table tr')) {
                    const tds = row.querySelectorAll('td');
                    const hasPdf = !!row.querySelector('a.icon-file-pdf.app_gridIcon');
                    const dataId = row.getAttribute('data-id');
                    if (tds.length >= 2) {
                        result.push({
                            dataId,
                            tds: tds.length,
                            hasPdf,
                            cells: [...tds].slice(0, 6).map(td => td.innerText.trim())
                        });
                    }
                }
                return result;
            }
        """)
        print(f"    Total filas con ≥2 celdas: {len(rows)}")
        for r in rows[:20]:
            print(f"      data-id={r['dataId']} tds={r['tds']} hasPdf={r['hasPdf']} | {r['cells'][:4]}")

        # Contar filas que pasarían el filtro del scraper
        validas = [r for r in rows if r["dataId"] and r["tds"] >= 6 and r["hasPdf"]]
        print(f"\n    Filas válidas para descarga (data-id + ≥6 celdas + PDF): {len(validas)}")
        for r in validas:
            print(f"      data-id={r['dataId']} | {r['cells']}")

        # ── 7. Ver contenido visible de la página ───────────────────────────
        info = await page.evaluate("""
            () => {
                const table = document.querySelector('table');
                const allTr = document.querySelectorAll('tr');
                const spinner = document.querySelector('[class*="spinner"], [class*="loading"], md-progress-circular');
                const msgVacio = [...document.querySelectorAll('*')]
                    .filter(el => el.offsetParent && /sin (datos|resultados)|no (hay|se encontr)/i.test(el.innerText))
                    .map(el => el.innerText.trim().substring(0, 80));
                return {
                    hasTable: !!table,
                    allTrCount: allTr.length,
                    hasSpinner: !!spinner,
                    msgVacio,
                    bodyText: document.body.innerText.substring(0, 600),
                };
            }
        """)
        print(f"\n    hasTable={info['hasTable']}  allTr={info['allTrCount']}  spinner={info['hasSpinner']}")
        print(f"    Mensajes vacío: {info['msgVacio']}")
        print(f"    Body (600 chars):\n{info['bodyText']}")

        print("\n[FIN] Revisá los screenshots en downloads/adcap_debug/")
        await asyncio.sleep(5)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

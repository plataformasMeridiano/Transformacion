"""
Exploración del portal BACS (Toronto Inversiones) — VBhome / Unisync.
Continúa desde la sección BOLETOS para ver los códigos de operación
y confirmar que el flujo es idéntico a ADCAP.
"""
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

URL_LOGIN = "https://alyc.torontoinversiones.com.ar/VBhome/login.html#!/login"
USUARIO   = "MeridianoNorte"
PASSWORD  = "Meridiano25$"


def sep(title: str) -> None:
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        # ── LOGIN ────────────────────────────────────────────────────────
        sep("1. LOGIN")
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("#input_0", timeout=30_000)
        await page.fill("#input_0", USUARIO)
        await page.fill("#input_1", PASSWORD)
        await page.click("#btnIngresar")
        await page.wait_for_url(lambda u: "#!/login" not in u, timeout=30_000)
        print(f"  Login OK — URL: {page.url}")

        # ── BOLETOS ──────────────────────────────────────────────────────
        sep("2. BOLETOS SIN FILTRO — todos los tipos de operación")
        await asyncio.sleep(3)
        await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('md-item-content[ng-click]'))
                    if (el.innerText.trim() === 'BOLETOS') { el.click(); break; }
            }
        """)
        await asyncio.sleep(4)
        print(f"  URL: {page.url}")

        # Volcar TODAS las filas con data-id para ver los códigos de operación
        all_rows = await page.evaluate("""
            () => [...document.querySelectorAll('table tr[data-id]')]
                    .map(tr => ({
                        dataId:  tr.getAttribute('data-id'),
                        cells:   [...tr.querySelectorAll('td')].map(td => td.innerText.trim()),
                        hasPdf:  !!tr.querySelector('a.icon-file-pdf.app_gridIcon'),
                        tdCount: tr.querySelectorAll('td').length,
                    }))
        """)
        print(f"  Total filas con data-id: {len(all_rows)}")
        print(f"  Con PDF: {sum(1 for r in all_rows if r['hasPdf'])}")
        print(f"\n  Filas de datos (con PDF) — primeras 20:")
        data_rows = [r for r in all_rows if r["hasPdf"]]
        for r in data_rows[:20]:
            print(f"    [{r['tdCount']} celdas] {r['cells'][:8]}")

        # ── FILTRAR POR FECHA ────────────────────────────────────────────
        sep("3. FILTRAR — 25/02/2026 a 28/02/2026")
        # Usar .first para evitar strict mode violation
        await page.locator("span.icon-filter").first.click()
        await asyncio.sleep(2)

        inputs_loc = page.locator(".md-dialog-container input.md-datepicker-input")
        n_inputs = await inputs_loc.count()
        print(f"  md-datepicker-input en dialog: {n_inputs}")

        await inputs_loc.nth(0).click()
        await inputs_loc.nth(0).select_text()
        await inputs_loc.nth(0).fill("25/02/2026")
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.4)

        await inputs_loc.nth(1).click()
        await inputs_loc.nth(1).select_text()
        await inputs_loc.nth(1).fill("28/02/2026")
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.4)

        await page.locator(".md-dialog-container button", has_text="FILTRAR").click()
        await asyncio.sleep(4)

        await page.screenshot(path="bacs_resultados.png", full_page=False)
        print("  Screenshot: bacs_resultados.png")

        # Volcar todas las filas del rango
        filtered_rows = await page.evaluate("""
            () => [...document.querySelectorAll('table tr[data-id]')]
                    .map(tr => ({
                        dataId:  tr.getAttribute('data-id'),
                        cells:   [...tr.querySelectorAll('td')].map(td => td.innerText.trim()),
                        hasPdf:  !!tr.querySelector('a.icon-file-pdf.app_gridIcon'),
                        tdCount: tr.querySelectorAll('td').length,
                    }))
        """)
        data_filtered = [r for r in filtered_rows if r["hasPdf"]]
        print(f"  Total filas: {len(filtered_rows)}  |  Con PDF: {len(data_filtered)}")

        # Mostrar TODAS las filas de datos para ver todos los códigos
        sep("4. TODAS LAS FILAS CON PDF (25/02–28/02) — para identificar códigos")
        col_counts = {}
        for r in data_filtered:
            print(f"  [{r['tdCount']} td] {r['cells']}")
            # Acumular todos los valores únicos de celdas para encontrar el código de tipo
            for c in r["cells"]:
                v = c.strip()
                if v and not v.replace("/", "").replace(".", "").isdigit():
                    col_counts[v] = col_counts.get(v, 0) + 1

        sep("5. VALORES NO-NUMÉRICOS ÚNICOS (frecuencia) → candidatos a código de tipo")
        for val, cnt in sorted(col_counts.items(), key=lambda x: -x[1]):
            print(f"  {cnt:3d}×  {repr(val)}")

        # ── INSPECCIONAR UN PDF DE CADA CÓDIGO DISTINTO ──────────────────
        sep("6. CLICK en PRIMER PDF para ver estructura del dialog")
        first_pdf_row = next((r for r in data_filtered), None)
        if first_pdf_row:
            did = first_pdf_row["dataId"]
            print(f"  data-id={did}  cells={first_pdf_row['cells']}")
            await page.click(f"tr[data-id='{did}'] a.icon-file-pdf.app_gridIcon")
            await asyncio.sleep(2)
            await page.screenshot(path="bacs_dialog_pdf.png", full_page=False)
            print("  Screenshot: bacs_dialog_pdf.png")

            dialog_links = await page.evaluate("""
                () => [...document.querySelectorAll('.md-dialog-container a')]
                        .map(a => ({
                            text:  a.innerText.trim(),
                            href:  a.getAttribute('href') || '',
                            cls:   a.className,
                        }))
            """)
            print("  Links en dialog:")
            for lnk in dialog_links:
                print(f"    cls={lnk['cls']!r}  href={lnk['href']!r}  text={lnk['text']!r}")

            await page.keyboard.press("Escape")
            await asyncio.sleep(1)
        else:
            print("  Sin filas con PDF")

        sep("7. Browser abierto 60s")
        print("  Cerrando en 60s...")
        await asyncio.sleep(60)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

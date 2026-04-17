"""
Debug WIN — verifica si el filtro de fecha funciona correctamente.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

with open("config.json") as f:
    cfg = json.load(f)

def resolve(v):
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], v)

alyc = next(a for a in cfg["alycs"] if a["nombre"] == "WIN")
OUT  = Path("downloads/win_debug")
OUT.mkdir(parents=True, exist_ok=True)

FECHA    = "02/03/2026"
BASE_URL = "https://clientes.winsa.com.ar"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=80)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page    = await context.new_page()

        # ── Login ─────────────────────────────────────────────────────────
        print("[1] Login...")
        await page.goto(alyc["url_login"], wait_until="load", timeout=30_000)
        await page.fill("input[name='Dni']", resolve(alyc["documento"]))
        await page.fill("#usuario",          resolve(alyc["usuario"]))
        await page.fill("#passwd",           resolve(alyc["contrasena"]))
        await page.click("#loginButton")
        await page.wait_for_url(lambda u: "/Login" not in u, timeout=30_000)
        print(f"    OK — {page.url}")

        # ── Navegar a PesosPorTipoOperacion ───────────────────────────────
        print("[2] Navegando a PesosPorTipoOperacion...")
        await page.goto(f"{BASE_URL}/Consultas/PesosPorTipoOperacion",
                        wait_until="load", timeout=30_000)
        await page.screenshot(path=str(OUT / "01_antes.png"), full_page=True)

        # Ver valores iniciales de los inputs
        val_desde = await page.input_value("#idInputFechaDesde")
        val_hasta = await page.input_value("#idInputFechaHasta")
        tipo_val  = await page.input_value("#idInputTipoCombo1")
        print(f"    FechaDesde inicial: {val_desde!r}")
        print(f"    FechaHasta inicial: {val_hasta!r}")
        print(f"    TipoCombo inicial:  {tipo_val!r}")

        # ── Setear filtros ────────────────────────────────────────────────
        print(f"[3] Seteando fecha {FECHA} y tipo Cauciones (03)...")
        await page.click("#idInputFechaDesde", click_count=3)
        await page.fill("#idInputFechaDesde", FECHA)
        await page.click("#idInputFechaHasta", click_count=3)
        await page.fill("#idInputFechaHasta", FECHA)
        await page.select_option("#idInputTipoCombo1", value="03")

        val_desde2 = await page.input_value("#idInputFechaDesde")
        val_hasta2 = await page.input_value("#idInputFechaHasta")
        print(f"    FechaDesde después: {val_desde2!r}")
        print(f"    FechaHasta después: {val_hasta2!r}")

        # ── Consultar ─────────────────────────────────────────────────────
        print("[4] Clickeando Consultar...")
        await page.click("button.boton-consulta")
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await page.screenshot(path=str(OUT / "02_resultado.png"), full_page=True)

        # ── Ver qué devolvió ──────────────────────────────────────────────
        filas = await page.evaluate("""
            () => {
                const rows = [];
                for (const tr of document.querySelectorAll('table tbody tr')) {
                    const tds = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                    const a   = tr.querySelector('a[href*="getComprobante"]');
                    const href = a ? a.getAttribute('href') : null;
                    rows.push({ cells: tds, href });
                }
                return rows;
            }
        """)
        print(f"\n    Filas en tabla: {len(filas)}")
        for r in filas[:10]:
            print(f"      {r['cells'][:5]}  href={r['href']}")
        if len(filas) > 10:
            print(f"      ... ({len(filas) - 10} más)")

        # Ver si los inputs se resetearon después del submit (ASP.NET puede hacer eso)
        val_desde3 = await page.input_value("#idInputFechaDesde")
        val_hasta3 = await page.input_value("#idInputFechaHasta")
        print(f"\n    FechaDesde post-consulta: {val_desde3!r}")
        print(f"    FechaHasta post-consulta: {val_hasta3!r}")

        print("\n[FIN] Screenshots en downloads/win_debug/")
        await asyncio.sleep(4)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

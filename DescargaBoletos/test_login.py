"""
Script de prueba de login para cualquier ALYC configurada.

Uso:
    python3 test_login.py              # prueba la primera ALYC activa en config.json
    python3 test_login.py ADCAP        # prueba una ALYC específica por nombre
"""

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from scrapers.alyc_sistemaA import PuenteScraper
from scrapers.alyc_sistemaB import AdcapScraper
from scrapers.alyc_sistemaC import WinScraper

# Cargar .env antes de cualquier otra cosa
load_dotenv()

SCRAPER_MAP = {
    "sistemaA": PuenteScraper,
    "sistemaB": AdcapScraper,
    "sistemaC": WinScraper,
}


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    with open(config_path) as f:
        return json.load(f)


def get_alyc_config(config: dict, nombre: str | None = None) -> dict:
    candidates = [a for a in config["alycs"] if a["activo"]]
    if not candidates:
        raise ValueError("No hay ALYCs activas en config.json")
    if nombre:
        match = next((a for a in candidates if a["nombre"] == nombre), None)
        if not match:
            raise ValueError(f"ALYC '{nombre}' no encontrada o no está activa")
        return match
    return candidates[0]


def build_scraper(alyc_config: dict, general_config: dict):
    sistema = alyc_config["sistema"]
    cls = SCRAPER_MAP.get(sistema)
    if not cls:
        raise ValueError(f"Sistema '{sistema}' no tiene scraper implementado. "
                         f"Sistemas disponibles: {list(SCRAPER_MAP)}")
    return cls(alyc_config, general_config)


KEYWORDS = {"boleto", "boletos", "operacion", "operaciones", "comprobante",
            "comprobantes", "reporte", "reportes", "descarga", "descargas",
            "liquidacion", "liquidaciones", "movimiento", "movimientos"}


async def inspect_post_login(page) -> None:
    # 1. URL actual
    print(f"\nURL post-login : {page.url}")

    # Esperar a que Angular termine de renderizar la vista post-login
    try:
        await page.wait_for_selector("[ng-view] *", timeout=10000)
    except Exception:
        pass  # Si no hay ng-view (portal clásico), continuar igual

    # 2. Screenshot
    screenshot_path = Path(__file__).parent / "post_login.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"Screenshot     : {screenshot_path}")

    # 3. Buscar secciones relevantes — cubre <a>, <button> y componentes Angular Material
    print("\n--- Secciones encontradas en la navegacion ---")
    selectors = (
        "nav a, header a, "
        "[class*='menu'] a, [class*='nav'] a, [class*='sidebar'] a, "
        "md-sidenav a, md-list-item, md-menu-item, "
        "[ng-view] button, [ng-view] a"
    )
    elementos = await page.query_selector_all(selectors)

    encontrados = []
    for el in elementos:
        texto = (await el.inner_text()).strip()
        href  = (await el.get_attribute("href") or "").strip()
        ng_click = (await el.get_attribute("ng-click") or "").strip()
        if not texto:
            continue
        if any(kw in texto.lower() for kw in KEYWORDS):
            encontrados.append((texto, href or ng_click))

    if encontrados:
        for texto, destino in encontrados:
            print(f"  [{texto}]  ->  {destino}")
    else:
        print("  Ninguno encontrado. Listando TODOS los elementos clickeables con texto...")
        todos = await page.query_selector_all("a, button, md-list-item, md-menu-item")
        for el in todos:
            texto = (await el.inner_text()).strip()
            if texto and len(texto) < 60:
                href     = (await el.get_attribute("href") or "").strip()
                ng_click = (await el.get_attribute("ng-click") or "").strip()
                print(f"  [{texto}]  ->  {href or ng_click or '(sin destino)'}")

    # 4. Mantener browser abierto para inspección visual
    print("\nCerrando browser en 10 segundos...")
    await asyncio.sleep(10)


async def main():
    config = load_config()
    general = config["general"]

    nombre_buscado = sys.argv[1] if len(sys.argv) > 1 else None
    alyc_config = get_alyc_config(config, nombre_buscado)

    print(f"ALYC    : {alyc_config['nombre']}")
    print(f"Sistema : {alyc_config['sistema']}")
    print(f"URL     : {alyc_config['url_login']}")
    print(f"headless: {general['headless']}")
    print("-" * 50)

    try:
        async with build_scraper(alyc_config, general) as scraper:
            success = await scraper.login()
            if success:
                print("OK — Login exitoso, sin PIN requerido")
                await inspect_post_login(scraper._page)

    except NotImplementedError as e:
        print(f"AVISO — {e}")
        sys.exit(2)

    except EnvironmentError as e:
        print(f"ERROR de configuracion — {e}")
        sys.exit(1)

    except Exception as e:
        print(f"ERROR inesperado — {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

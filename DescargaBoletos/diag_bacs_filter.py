"""
diag_bacs_filter.py — Diagnóstico del filtro de fecha en el portal BACS (Toronto Inversiones).

Prueba tres fechas:
  - Una "vieja" que funciona (ene-28, >40 días)
  - Dos del "gap" (feb-20, feb-25, ~13-25 días atrás)

Para cada fecha:
  1. Navega a BOLETOS
  2. Reporta si span.icon-filter existe y su estado visible
  3. Intenta abrir el filtro y reporta el resultado ('ok', 'no-dialog', 'no-filters', etc.)
  4. Reporta cuántas filas hay en la tabla ANTES y DESPUÉS del filtro
  5. Vuelca los primeros 3 rows para inspección

Uso:
    python3 diag_bacs_filter.py [fecha1 fecha2 ...]
    # default: 2026-01-28 2026-02-20 2026-02-25
"""
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("diag_bacs")

from scrapers.alyc_sistemaB import AdcapScraper  # noqa: E402

FECHAS_DEFAULT = ["2026-01-28", "2026-02-20", "2026-02-25"]

ALYC_CONFIG = {
    "nombre": "BACS",
    "sistema": "sistemaB",
    "url_login": "https://alyc.torontoinversiones.com.ar/VBhome/login.html#!/login",
    "usuario": "${BACS_USUARIO}",
    "contrasena": "${BACS_PASSWORD}",
    "opciones": {
        "caucion_codes": [],
        "tipo_operacion": ["Pases"],
        "timeout_ms": 30_000,
    },
}
GENERAL_CONFIG = {"headless": True, "download_dir": "downloads"}


async def inspect_filter_state(page, fecha_iso: str) -> dict:
    """Devuelve info sobre el estado del filtro y la tabla."""
    fecha_fmt = datetime.strptime(fecha_iso, "%Y-%m-%d").strftime("%d/%m/%Y")

    # ─── estado inicial: filas en tabla ─────────────────────────────────────
    rows_before = await page.evaluate("""
        () => {
            const rows = [...document.querySelectorAll('table tr[data-id]')];
            return rows.map(r => {
                const cells = [...r.querySelectorAll('td')].map(td => td.innerText.trim());
                return { dataId: r.getAttribute('data-id'), cells };
            });
        }
    """)

    # ─── estado del botón filtro ─────────────────────────────────────────────
    filter_info = await page.evaluate("""
        () => {
            const ic = document.querySelector('span.icon-filter');
            if (!ic) return { exists: false };
            const style = window.getComputedStyle(ic);
            return {
                exists: true,
                display: style.display,
                visibility: style.visibility,
                offsetParent: ic.offsetParent !== null,
            };
        }
    """)

    # ─── intentar abrir el filtro ────────────────────────────────────────────
    await page.evaluate("""
        () => {
            const ic = document.querySelector('span.icon-filter');
            if (ic) ic.click();
        }
    """)
    import asyncio
    await asyncio.sleep(2)

    # ─── estado del dialog ───────────────────────────────────────────────────
    dialog_info = await page.evaluate("""
        () => {
            const d = document.querySelector('.md-dialog-container');
            if (!d) return { exists: false };
            const scope = typeof angular !== 'undefined'
                ? angular.element(d).scope()
                : null;
            return {
                exists: true,
                has_scope: !!scope,
                has_filters: !!(scope && scope.filters),
                filters: scope && scope.filters ? {
                    fechaDesde: scope.filters.fechaDesde ? String(scope.filters.fechaDesde) : null,
                    fechaHasta: scope.filters.fechaHasta ? String(scope.filters.fechaHasta) : null,
                } : null,
                dialog_html_snippet: d.innerHTML.substring(0, 400),
            };
        }
    """)

    # ─── si el dialog existe, aplicar el filtro ──────────────────────────────
    filter_result = "no-dialog"
    rows_after = rows_before
    if dialog_info.get("exists"):
        filter_result = await page.evaluate(f"""
            () => {{
                const dialog = document.querySelector('.md-dialog-container');
                if (!dialog) return 'no-dialog';
                const scope = angular.element(dialog).scope();
                if (!scope?.filters) return 'no-filters';
                const d = new Date('{fecha_iso}T12:00:00-03:00');
                scope.filters.fechaDesde = d;
                scope.filters.fechaHasta = d;
                scope.$apply();
                scope.filter();
                return 'ok';
            }}
        """)
        await asyncio.sleep(3)

        rows_after = await page.evaluate(f"""
            () => {{
                const fecha = '{fecha_fmt}';
                const rows = [...document.querySelectorAll('table tr[data-id]')];
                return rows.map(r => {{
                    const cells = [...r.querySelectorAll('td')].map(td => td.innerText.trim());
                    return {{
                        dataId: r.getAttribute('data-id'),
                        cells,
                        matchesFecha: cells[2] === fecha,
                    }};
                }});
            }}
        """)

        # Cerrar el dialog si sigue abierto
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)

    # ─── también ver cuántas filas totales hay en la tabla ───────────────────
    all_rows = await page.evaluate("""
        () => document.querySelectorAll('table tr').length
    """)

    return {
        "filter_btn": filter_info,
        "rows_before": len(rows_before),
        "rows_before_sample": rows_before[:3],
        "dialog": dialog_info,
        "filter_result": filter_result,
        "rows_after": len(rows_after),
        "rows_after_matching_fecha": sum(1 for r in rows_after if r.get("matchesFecha")),
        "rows_after_sample": [r for r in rows_after[:3]],
        "total_tr_count": all_rows,
    }


async def main():
    fechas = sys.argv[1:] if len(sys.argv) > 1 else FECHAS_DEFAULT

    async with AdcapScraper(ALYC_CONFIG, GENERAL_CONFIG) as scraper:
        logger.info("Haciendo login en BACS...")
        await scraper.login()
        page = scraper._page

        logger.info("Esperando que el dashboard cargue...")
        import asyncio
        await asyncio.sleep(5)

        for fecha in fechas:
            logger.info("=" * 60)
            logger.info("FECHA: %s", fecha)

            # Navegar a BOLETOS
            await scraper._navegar_boletos(30_000)
            logger.info("Vista BOLETOS cargada.")

            info = await inspect_filter_state(page, fecha)

            logger.info("  icon-filter: exists=%s display=%s visible=%s",
                        info["filter_btn"].get("exists"),
                        info["filter_btn"].get("display"),
                        info["filter_btn"].get("offsetParent"))
            logger.info("  rows ANTES del filtro: %d  (total tr: %d)",
                        info["rows_before"], info["total_tr_count"])
            logger.info("  dialog exists: %s  filter_result: %s",
                        info["dialog"].get("exists"), info["filter_result"])
            logger.info("  rows DESPUÉS del filtro: %d  (matching fecha: %d)",
                        info["rows_after"], info["rows_after_matching_fecha"])

            if info["rows_before_sample"]:
                logger.info("  Sample rows ANTES:")
                for r in info["rows_before_sample"]:
                    logger.info("    data-id=%-12s  cells=%s", r["dataId"], r["cells"])
            else:
                logger.info("  Sin rows antes del filtro.")

            if info["filter_result"] == "ok" and info["rows_after_sample"]:
                logger.info("  Sample rows DESPUÉS:")
                for r in info["rows_after_sample"]:
                    logger.info("    data-id=%-12s  match=%s  cells=%s",
                                r["dataId"], r.get("matchesFecha"), r["cells"])

            if info["dialog"].get("filters"):
                logger.info("  Dialog filters: %s", info["dialog"]["filters"])

        logger.info("=" * 60)
        logger.info("Diagnóstico completo.")


asyncio.run(main())

"""
run_allaria_cobros_fce.py — Detecta cobros de FCE-eCheq en la vista monetaria de Allaria
y dispara webhook Zapier por cada cobro detectado en el rango de fechas.

La vista monetaria (#!/monetaria) muestra movimientos de cuenta corriente.
Los cobros FCE aparecen con descripción "COBRO CHEQUE %INC<codigo_mav>".

Uso:
    python3 run_allaria_cobros_fce.py                               # delta: últimos 7 días hábiles
    python3 run_allaria_cobros_fce.py --desde 2026-04-01 --hasta 2026-04-24
"""
import asyncio
import json
import logging
import os
import re
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from scrapers.alyc_sistemaH import AllariaScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("allaria_cobros_fce")

ZAPIER_WEBHOOK = "https://hooks.zapier.com/hooks/catch/24963922/uv6jb45/"

# "COBRO CHEQUE %INC100400020" → grupo 1 = "INC100400020"
# El código MAV puede ser de cualquier empresa (no solo INC)
_COBRO_RE = re.compile(r"COBRO\s+CHEQUE\s+%(\S+)", re.IGNORECASE)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _last_n_business_days(n: int) -> tuple[str, str]:
    result, d = [], date.today() - timedelta(days=1)
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d.isoformat())
        d -= timedelta(days=1)
    result.reverse()
    return result[0], result[-1]


def _parse_importe(s: str) -> float:
    """'$ 29.306.831,44' → 29306831.44"""
    s = s.strip().lstrip("$").strip().replace(".", "").replace(",", ".")
    return float(s) if s else 0.0


def _parse_nro_boleto(s: str) -> int:
    """'543.984' → 543984"""
    s = s.strip().replace(".", "")
    return int(s) if s.isdigit() else 0


def _fmt_fecha(s: str) -> str:
    """'13/04/2026' → '2026-04-13'"""
    parts = s.strip().split("/")
    return f"{parts[2]}-{parts[1]}-{parts[0]}" if len(parts) == 3 else s


def _fire_webhook(cobro: dict) -> bool:
    data = json.dumps(cobro).encode()
    req = urllib.request.Request(
        ZAPIER_WEBHOOK, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        logger.error("Error en webhook: %s", e)
        return False


def _resolve_cfg(obj):
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], obj)
    if isinstance(obj, dict):
        return {k: _resolve_cfg(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_cfg(v) for v in obj]
    return obj


# ── Scraping ────────────────────────────────────────────────────────────────────

async def scrape_cobros_fce(scraper: AllariaScraper, desde_iso: str, hasta_iso: str) -> list[dict]:
    """
    Navega a la vista #!/monetaria de Allaria y devuelve los cobros FCE
    en el rango [desde_iso, hasta_iso].

    Columnas esperadas de la tabla de movimientos:
        0: fecha_concertacion (dd/mm/yyyy)
        1: fecha_liquidacion  (dd/mm/yyyy)
        2: descripcion
        3: tipo_cambio        (ignorar)
        4: importe            ($ NNN.NNN,NN)
        5: nro_boleto         (NNN.NNN)
        6: saldo              (ignorar)
    """
    page = scraper._page
    timeout = 30_000

    # 1. Navegar a #!/monetaria via scope Angular (mismo patrón que boletos)
    result = await page.evaluate("""
        () => {
            const el = document.querySelector('[ng-controller]');
            if (!el) return 'no-ng-controller';
            const scope = angular.element(el).scope();
            if (!scope) return 'no-scope';
            if (typeof scope.changeCurrentView !== 'function') return 'no-changeCurrentView';
            scope.changeCurrentView('/monetaria', null, false, false);
            return 'ok';
        }
    """)
    logger.info("Navegación a /monetaria: %s — URL: %s", result, page.url)
    await asyncio.sleep(4)
    logger.info("URL monetaria: %s", page.url)

    # 2. Aplicar filtro de fechas via el diálogo de filtros (icon-filter)
    await page.evaluate("() => { const ic = document.querySelector('span.icon-filter'); if (ic) ic.click(); }")
    await asyncio.sleep(1.5)

    filter_result = await page.evaluate(f"""
        () => {{
            const dialog = document.querySelector('.md-dialog-container');
            const target  = dialog || document.querySelector('[ng-controller]');
            if (!target) return 'no-target';
            const scope = angular.element(target).scope();
            if (!scope?.filters) return 'no-filters';
            const desde = new Date('{desde_iso}T12:00:00-03:00');
            const hasta = new Date('{hasta_iso}T12:00:00-03:00');
            scope.filters.fechaDesde = desde;
            scope.filters.fechaHasta = hasta;
            if (scope.filters.tipoFecha !== undefined) scope.filters.tipoFecha = -1;
            scope.$apply();
            if (typeof scope.filter === 'function') scope.filter();
            return dialog ? 'dialog-ok' : 'root-ok';
        }}
    """)
    logger.info("Filtro de fechas (%s → %s): %s", desde_iso, hasta_iso, filter_result)
    await asyncio.sleep(3)
    await page.keyboard.press("Escape")
    await asyncio.sleep(1)

    # 3. Leer filas de la tabla
    rows_raw: list[list[str]] = await page.evaluate("""
        () => {
            const results = [];
            // VBolsaNet usa tablas HTML estándar para grillas
            for (const row of document.querySelectorAll('table tbody tr')) {
                const cells = Array.from(row.querySelectorAll('td'))
                    .map(td => td.innerText?.trim() ?? '');
                if (cells.length >= 5) results.push(cells);
            }
            return results;
        }
    """)
    logger.info("Filas leídas de la tabla: %d", len(rows_raw))

    # 4. Filtrar y parsear cobros FCE
    cobros: list[dict] = []
    for cells in rows_raw:
        # Buscar el patrón COBRO CHEQUE %INC... en cualquier columna
        desc = next((c for c in cells if _COBRO_RE.search(c)), None)
        if not desc:
            continue

        m = _COBRO_RE.search(desc)
        fce_code = m.group(1)

        try:
            # Estructura de la tabla (11 columnas):
            # [0]=vacío [1]=nro_fila [2]=fecha_conc [3]=fecha_liq [4]=descripcion
            # [5]=vacío [6]=vacío [7]=tipo_cambio [8]=importe [9]=nro_boleto [10]=saldo
            fecha_conc_iso = _fmt_fecha(cells[2])
            fecha_liq_iso  = _fmt_fecha(cells[3])

            # Filtrar por rango (client-side, por si el filtro del portal no funcionó)
            if fecha_conc_iso < desde_iso or fecha_conc_iso > hasta_iso:
                continue

            importe    = _parse_importe(cells[8]) if len(cells) > 8 else 0.0
            nro_boleto = _parse_nro_boleto(cells[9]) if len(cells) > 9 else 0

            cobro = {
                "fecha_concertacion": fecha_conc_iso,
                "fecha_liquidacion":  fecha_liq_iso,
                "fce":                fce_code,
                "importe":            importe,
                "nro_boleto":         nro_boleto,
            }
            cobros.append(cobro)
            logger.info("Cobro FCE: %s / %s / fce=%s / importe=%s / nro=%d",
                        fecha_conc_iso, fecha_liq_iso, fce_code, importe, nro_boleto)

        except Exception as e:
            logger.warning("Error parseando fila %s: %s", cells, e)

    return cobros


# ── Main ────────────────────────────────────────────────────────────────────────

async def main() -> int:
    args = sys.argv[1:]
    desde_iso = hasta_iso = None

    if "--desde" in args:
        desde_iso = args[args.index("--desde") + 1]
    if "--hasta" in args:
        hasta_iso = args[args.index("--hasta") + 1]

    if not desde_iso:
        desde_iso, hasta_iso = _last_n_business_days(7)
    if not hasta_iso:
        hasta_iso = desde_iso

    logger.info("Cobros FCE Allaria: %s → %s", desde_iso, hasta_iso)

    with open("config.json") as f:
        full_cfg = json.load(f)

    general_cfg  = _resolve_cfg(full_cfg["general"])
    allaria_raw  = next(a for a in full_cfg["alycs"] if a["nombre"] == "Allaria")
    allaria_cfg  = _resolve_cfg(allaria_raw)

    async with AllariaScraper(allaria_cfg, general_cfg) as scraper:
        await scraper.login()
        logger.info("Login Allaria OK")
        cobros = await scrape_cobros_fce(scraper, desde_iso, hasta_iso)

    logger.info("Cobros FCE encontrados: %d", len(cobros))

    ok = err = 0
    for cobro in cobros:
        if _fire_webhook(cobro):
            logger.info("Webhook OK — fce=%s fecha=%s", cobro["fce"], cobro["fecha_concertacion"])
            ok += 1
        else:
            logger.error("Webhook FAILED — fce=%s", cobro["fce"])
            err += 1

    _save_cobros(cobros)

    logger.info("TOTAL: %d OK / %d errores", ok, err)
    return 0 if err == 0 else 1


def _save_cobros(cobros: list[dict]) -> None:
    """Persiste los cobros en cobros/{fecha}.json para verificación posterior."""
    cobros_dir = Path(__file__).parent / "cobros"
    cobros_dir.mkdir(exist_ok=True)

    by_fecha: dict[str, list[dict]] = {}
    for c in cobros:
        by_fecha.setdefault(c["fecha_concertacion"], []).append(c)

    for fecha, lista in by_fecha.items():
        path = cobros_dir / f"{fecha}.json"
        existing: list[dict] = json.loads(path.read_text()) if path.exists() else []
        seen = {c["nro_boleto"] for c in existing}
        nuevos = [c for c in lista if c["nro_boleto"] not in seen]
        if nuevos:
            path.write_text(json.dumps(existing + nuevos, indent=2, ensure_ascii=False))
            logger.info("Cobros guardados en %s: %d nuevos", path.name, len(nuevos))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

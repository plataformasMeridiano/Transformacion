"""
Test de descarga para una fecha dada — sin subida a Drive.
Uso:
    python3 test_descarga_dia.py 2026-03-02
    python3 test_descarga_dia.py          # usa ayer por defecto

Valida:
    - Al menos 2 Cauciones por ALYC
    - No más de 5 archivos por ALYC
    - Entre 0 y 4 Pases por ALYC
"""
import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from scrapers.alyc_sistemaA import PuenteScraper
from scrapers.alyc_sistemaB import AdcapScraper
from scrapers.alyc_sistemaC import WinScraper
from scrapers.alyc_sistemaD import ConoSurScraper
from scrapers.alyc_sistemaE import MaxCapitalScraper
from scrapers.alyc_sistemaF import MetroCorpScraper

SCRAPER_MAP = {
    "sistemaA": PuenteScraper,
    "sistemaB": AdcapScraper,
    "sistemaC": WinScraper,
    "sistemaD": ConoSurScraper,
    "sistemaE": MaxCapitalScraper,
    "sistemaF": MetroCorpScraper,
}

# ALYCs a saltear (ban Imperva activo)
SKIP = {"Puente"}


def _validar(por_tipo: dict[str, list]) -> list[str]:
    alertas = []
    cauciones = len(por_tipo.get("Cauciones", []))
    pases     = len(por_tipo.get("Pases", []))
    total     = sum(len(v) for v in por_tipo.values())

    if cauciones < 2:
        alertas.append(f"⚠  Cauciones={cauciones} (esperado ≥2)")
    if total > 5:
        alertas.append(f"⚠  Total={total} archivos (esperado ≤5)")
    if pases > 4:
        alertas.append(f"⚠  Pases={pases} (esperado ≤4)")
    return alertas


async def procesar(alyc_cfg: dict, general_cfg: dict, fecha: str, dest_base: Path):
    nombre = alyc_cfg["nombre"]
    sistema = alyc_cfg["sistema"]
    cls = SCRAPER_MAP[sistema]

    dest_dir = dest_base / nombre / fecha
    dest_dir.mkdir(parents=True, exist_ok=True)

    async with cls(alyc_cfg, general_cfg) as scraper:
        await scraper.login()
        return await scraper.download_tickets(fecha, dest_dir)


async def main():
    if len(sys.argv) > 1:
        fecha = sys.argv[1]
    else:
        fecha = (date.today() - timedelta(days=1)).isoformat()

    # Solo errores del propio test; los scrapers loguean internamente
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s  %(name)s  %(message)s",
    )
    # Silenciar loggers de scrapers en consola (igual quedan en archivo si hay FileHandler)
    for name in ("scrapers.alyc_sistemaA", "scrapers.alyc_sistemaB",
                 "scrapers.alyc_sistemaC", "scrapers.alyc_sistemaD",
                 "scrapers.alyc_sistemaE", "scrapers.alyc_sistemaF"):
        logging.getLogger(name).setLevel(logging.WARNING)

    with open("config.json") as f:
        config = json.load(f)

    general = config["general"]  # headless=False — no forzar True como en producción
    dest_base = Path("downloads/test")

    alycs = [
        a for a in config["alycs"]
        if a.get("activo") and a["nombre"] not in SKIP
    ]

    print(f"\n{'='*55}")
    print(f"  Test descarga — {fecha}")
    print(f"  ALYCs: {[a['nombre'] for a in alycs]}")
    print(f"{'='*55}\n")

    resumen = []

    for alyc_cfg in alycs:
        nombre = alyc_cfg["nombre"]
        print(f"▶  {nombre}  ...", end="", flush=True)

        try:
            pdfs = await procesar(alyc_cfg, general, fecha, dest_base)
        except Exception as exc:
            print(f"\r✗  {nombre:<14}  ERROR: {type(exc).__name__}: {exc}")
            resumen.append((nombre, None, [f"ERROR: {exc}"]))
            continue

        por_tipo: dict[str, list] = {}
        for p in pdfs:
            por_tipo.setdefault(p.parent.name, []).append(p.name)

        alertas = _validar(por_tipo)
        estado  = "✓" if not alertas else "⚠"
        total   = sum(len(v) for v in por_tipo.values())

        linea = f"\r{estado}  {nombre:<14}  total={total}"
        for tipo, nombres in sorted(por_tipo.items()):
            linea += f"   {tipo}={len(nombres)}"
        print(linea)

        for a in alertas:
            print(f"       {a}")

        for tipo, nombres in sorted(por_tipo.items()):
            for n in nombres:
                print(f"       {tipo}/{n}")

        resumen.append((nombre, por_tipo, alertas))

    # ── Resumen final ─────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  RESUMEN — {fecha}")
    print(f"{'='*55}")
    for nombre, por_tipo, alertas in resumen:
        if por_tipo is None:
            print(f"  ✗  {nombre}")
        elif alertas:
            print(f"  ⚠  {nombre}  — {'; '.join(alertas)}")
        else:
            detalle = "  ".join(f"{t}={len(v)}" for t, v in sorted(por_tipo.items()))
            print(f"  ✓  {nombre}  — {detalle}")
    print()


if __name__ == "__main__":
    asyncio.run(main())

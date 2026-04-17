"""
diag_dhalmore_codes.py — Lista todos los tipos de movimiento que devuelve
la API de Dhalmore para un rango de fechas.

Uso:
    python3 diag_dhalmore_codes.py 2026-04-01 2026-04-16
"""
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

_PROFILE_DIR = Path("browser_profiles/dhalmore")
_BASE        = "https://apiv2.fermi.com.ar"
_DEVICE_ID   = "49dde3e5-bae6-4067-9930-5f213a2468a8"
_TIMEOUT     = 30_000

# Tipos conocidos y algunos extra para explorar
_TYPES_TO_TRY = ["CAUCION", "PASS", "FCE", "CHEQUE", "ECHEQ", "PAGARE",
                 "NEGOCIACION", "COMPRAVENTA", "BURSATIL"]


async def main():
    load_dotenv(Path(__file__).parent / ".env")

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    alycs = [a for a in config["alycs"] if a["nombre"] == "Dhalmore"]
    if not alycs:
        logger.error("No se encontró Dhalmore en config.json")
        return

    alyc_cfg = alycs[0]
    cuentas  = alyc_cfg["opciones"]["cuentas"]

    inicio = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 4, 1)
    fin    = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()

    logger.info("Dhalmore — explorando tipos de movimiento %s → %s", inicio, fin)

    # Login via browser para obtener el Bearer token
    bearer = None

    async def on_request(req):
        nonlocal bearer
        auth = req.headers.get("authorization", "")
        if auth.startswith("Bearer ") and len(auth) > 20:
            bearer = auth

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=True,
            executable_path="/usr/bin/google-chrome-stable",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("request", on_request)

        usuario   = alyc_cfg["usuario"].replace("${", "").replace("}", "")
        contrasena = alyc_cfg["contrasena"].replace("${", "").replace("}", "")

        import os
        usuario   = os.environ.get(alyc_cfg["usuario"].strip("${}"), alyc_cfg["usuario"])
        contrasena = os.environ.get(alyc_cfg["contrasena"].strip("${}"), alyc_cfg["contrasena"])

        await page.goto("https://clientes.dhalmorecap.com/", wait_until="load", timeout=_TIMEOUT)
        await page.wait_for_timeout(3000)

        # Login si es necesario
        if "login" in page.url.lower() or await page.locator("input[type='email'], input[type='text']").count() > 0:
            logger.info("Login requerido...")
            await page.locator("input[type='email'], input[type='text']").first.fill(usuario)
            await page.locator("input[type='password']").first.fill(contrasena)
            await page.locator("button[type='submit']").first.click()
            await page.wait_for_timeout(5000)

        # Esperar hasta capturar Bearer
        for _ in range(10):
            if bearer:
                break
            await page.wait_for_timeout(1000)

        await ctx.close()

    if not bearer:
        logger.error("No se pudo capturar el Bearer token")
        return

    logger.info("Bearer capturado — explorando API")

    all_codes: Counter = Counter()

    async with httpx.AsyncClient(timeout=30) as client:
        headers = {
            "Authorization": bearer,
            "device-id": _DEVICE_ID,
            "Content-Type": "application/json",
        }

        for cuenta in cuentas:
            cta_id   = cuenta["customer_account_id"]
            cta_name = cuenta["nombre"]

            logger.info("\n--- Cuenta: %s (id=%s) ---", cta_name, cta_id)

            # Iterar fechas
            d = inicio
            while d <= fin:
                next_d = d + timedelta(days=1)
                fd = d.strftime("%Y-%m-%dT00:00:00.000Z")
                fh = next_d.strftime("%Y-%m-%dT00:00:00.000Z")

                # Probar cada tipo
                for tipo in _TYPES_TO_TRY:
                    url = (f"{_BASE}/checking-accounts/customer-account/{cta_id}"
                           f"/historical-movements?currency=ARS&type={tipo}"
                           f"&fromDate={fd}&toDate={fh}")
                    try:
                        resp = await client.get(url, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            items = data if isinstance(data, list) else data.get("content", data.get("data", []))
                            if items:
                                logger.info("  %s [%s] type=%-15s → %d items",
                                            d, cta_name, tipo, len(items))
                                for item in items[:3]:
                                    op_code = item.get("operationCode", item.get("code", item.get("type", "?")))
                                    desc = item.get("description", item.get("concept", ""))
                                    all_codes[f"{tipo}:{op_code}"] += 1
                                    logger.info("    code=%s  desc=%s", op_code, str(desc)[:60])
                    except Exception as e:
                        logger.debug("  %s type=%s error: %s", d, tipo, e)

                d = next_d

    logger.info("\n=== RESUMEN CÓDIGOS ===")
    for code, count in all_codes.most_common():
        logger.info("  %-30s  %d veces", code, count)


if __name__ == "__main__":
    asyncio.run(main())

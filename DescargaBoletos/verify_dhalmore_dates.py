"""
verify_dhalmore_dates.py — Verifica qué representa operationDate en historical-movements
comparándolo con day-movements para la misma fecha.

Abre el browser para capturar el bearer, luego consulta ambos endpoints via httpx.
"""
import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(Path(__file__).parent / ".env")

_PROFILE_DIR = Path("browser_profiles/dhalmore")
_API_BASE    = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
_URL_BASE    = "https://clientes.dhalmorecap.com/"
_USUARIO     = os.environ.get("DHALMORE_USUARIO", "")
_PASSWORD    = os.environ.get("DHALMORE_PASSWORD", "")
_DEVICE_ID   = "49dde3e5-bae6-4067-9930-5f213a2468a8"
_CID         = 56553  # MeridianoNorte

# Fecha a verificar — una con cauciones conocidas
TEST_DATE = "2026-02-27"

_HEADERS_EXTRA = {
    "x-device-id":                 _DEVICE_ID,
    "x-use-wrapped-single-values": "true",
    "x-client-name":               "WEB 0.38.2",
    "Origin":                      "https://clientes.dhalmorecap.com",
    "Referer":                     "https://clientes.dhalmorecap.com/",
}


async def get_bearer() -> str:
    bearer = None
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(_PROFILE_DIR), headless=False, slow_mo=50,
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_request(req):
            nonlocal bearer
            if "fermi" in req.url and req.resource_type in ("xhr", "fetch"):
                auth = req.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    bearer = auth

        page.on("request", on_request)

        await page.goto(_URL_BASE, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(2000)

        if "auth0" in page.url:
            print("[login] Haciendo login...")
            await page.wait_for_selector("input[name='username']", timeout=15_000)
            await page.fill("input[name='username']", _USUARIO)
            await page.fill("input[name='password']", _PASSWORD)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(6000)

        # Esperar a tener bearer
        for _ in range(30):
            if bearer:
                break
            await page.wait_for_timeout(1000)

        await ctx.close()

    return bearer


async def main():
    print("Capturando bearer token...")
    bearer = await get_bearer()
    if not bearer:
        print("ERROR: no se pudo capturar bearer")
        return

    print(f"Bearer OK: {bearer[:40]}...")

    headers = {
        "Authorization": bearer,
        "Accept": "application/json, text/plain, */*",
        **_HEADERS_EXTRA,
    }

    fecha = TEST_DATE
    from_dt = datetime.strptime(fecha, "%Y-%m-%d")

    async with httpx.AsyncClient(timeout=30) as client:

        # 1. day-movements para la fecha
        print(f"\n{'='*60}")
        print(f"day-movements?date={fecha}")
        r = await client.get(
            f"{_API_BASE}/checking-accounts/customer-account/{_CID}/day-movements",
            headers=headers, params={"date": fecha}
        )
        day_data = r.json()
        day_numbers = {}
        for item in day_data.get("content", []):
            for detail in item.get("details", []):
                op = detail.get("operation", "")
                if any(k in op.upper() for k in ("CAUCION", "PASE", "PASS")):
                    num = detail.get("number")
                    day_numbers[num] = op
                    print(f"  number={num:8s}  operation={op}")

        print(f"\nTotal boletos caucion/pase en day-movements: {len(day_numbers)}")

        # 2. historical-movements con rango CORTO (original: fecha → fecha+1)
        print(f"\n{'='*60}")
        to_date_short = (from_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"historical-movements RANGO CORTO: {fecha} → {to_date_short}")
        for tipo_api in ("CAUCION", "PASS"):
            for currency in ("ARS", "USD"):
                r = await client.get(
                    f"{_API_BASE}/checking-accounts/customer-account/{_CID}/historical-movements",
                    headers=headers,
                    params={
                        "currency": currency,
                        "type": tipo_api,
                        "fromDate": f"{fecha}T00:00:00.000Z",
                        "toDate": f"{to_date_short}T00:00:00.000Z",
                    }
                )
                data = r.json()
                content = data.get("content", data) if isinstance(data, dict) else data
                movs = [m for m in content if isinstance(m, dict)]
                for m in movs:
                    print(f"  [{tipo_api}/{currency}] receiptCode={m.get('receiptCode')}  "
                          f"orderCode={m.get('orderCode')}  "
                          f"operationDate={m.get('operationDate')}  "
                          f"documentKey={str(m.get('documentKey',''))[:20]}")

        # 3. historical-movements con rango AMPLIO (fecha → fecha+60)
        print(f"\n{'='*60}")
        to_date_wide = (from_dt + timedelta(days=60)).strftime("%Y-%m-%d")
        print(f"historical-movements RANGO AMPLIO: {fecha} → {to_date_wide}")
        found_wide = {}
        for tipo_api in ("CAUCION", "PASS"):
            for currency in ("ARS", "USD"):
                r = await client.get(
                    f"{_API_BASE}/checking-accounts/customer-account/{_CID}/historical-movements",
                    headers=headers,
                    params={
                        "currency": currency,
                        "type": tipo_api,
                        "fromDate": f"{fecha}T00:00:00.000Z",
                        "toDate": f"{to_date_wide}T00:00:00.000Z",
                    }
                )
                data = r.json()
                content = data.get("content", data) if isinstance(data, dict) else data
                movs = [m for m in content if isinstance(m, dict)
                        and m.get("operationDate") == fecha]
                for m in movs:
                    rc = str(m.get("receiptCode", ""))
                    found_wide[rc] = m
                    in_day = "✓ MATCH" if rc in day_numbers else "✗ NO MATCH"
                    oc = str(m.get("orderCode", ""))
                    in_day = "✓ MATCH" if oc in day_numbers else "✗ NO MATCH"
                    print(f"  [{tipo_api}/{currency}] receiptCode={rc:8s}  orderCode={oc:8s}  "
                          f"operationDate={m.get('operationDate')}  "
                          f"documentKey={str(m.get('documentKey',''))[:20]}  {in_day}")

        print(f"\n{'='*60}")
        print(f"day-movements numbers:          {sorted(day_numbers.keys())}")
        print(f"historical wide (opDate={fecha}): {sorted(found_wide.keys())}")
        missing = set(day_numbers.keys()) - set(found_wide.keys())
        if missing:
            print(f"FALTAN en historical-wide: {missing}")
        else:
            print("Todos los boletos de day-movements aparecen en historical-wide ✓")


if __name__ == "__main__":
    asyncio.run(main())

"""
Verificación rápida del endpoint correcto de comprobantes ConoSur:
  GET /api/comprobantes/{nro_url_encoded}?formato=PDF
"""
import asyncio
import os
from urllib.parse import quote

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

URL_LOGIN = "https://virtualbroker-conosur.aunesa.com/auth/signin"
API_BASE  = "https://vb-back-conosur.aunesa.com/api"
CUENTA    = "3003"
USUARIO   = "meridianonorte"
PASSWORD  = "12345"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        # Login
        await page.goto(URL_LOGIN, wait_until="networkidle", timeout=60_000)
        await page.click("#usuario")
        await page.keyboard.type(USUARIO, delay=40)
        await page.click("#contraseña")
        await page.keyboard.type(PASSWORD, delay=40)
        await page.locator("button[type='submit']").click()
        await page.wait_for_url(lambda u: "/auth/signin" not in u, timeout=20_000)

        # Token
        sess = await (await context.request.get(
            "https://virtualbroker-conosur.aunesa.com/api/auth/session"
        )).json()
        auth_h = {"Authorization": f"Bearer {sess['accessToken']}"}

        # Obtener movimientos para el 25/02/2026
        # Usar fechaHasta más amplio para capturar liquidaciones del día siguiente
        resp = await context.request.get(
            f"{API_BASE}/v2/cuentas/{CUENTA}/movimientos",
            params={
                "fechaDesde": "25/02/2026",
                "fechaHasta": "03/03/2026",
                "tipoMovimiento": "monetarios",
                "page": "1",
                "size": "100",
                "estado": "DIS",
                "especie": "ARS",
            },
            headers=auth_h,
        )
        data = await resp.json()
        movs = data.get("movimientos", {}).get("content", [])
        print(f"Movimientos en rango 25/02-03/03: {len(movs)}")
        for m in movs:
            print(f"  liq={m['liquidacion']}  conc={m['concertacion']}  "
                  f"concepto={m['concepto']!r}  nro={m['numeroComprobante']!r}")

        # Filtrar los concertados el 25/02
        target = [m for m in movs if m["concertacion"] == "25/02/2026"]
        print(f"\nConcertados el 25/02/2026: {len(target)}")

        # Descargar el PDF del primero
        os.makedirs("downloads/ConoSur_test", exist_ok=True)
        for m in target[:5]:
            nro = m["numeroComprobante"]
            url = f"{API_BASE}/comprobantes/{quote(nro)}?formato=PDF"
            print(f"\nDescargando {nro!r} → {url}")
            r = await context.request.get(url, headers=auth_h, timeout=15_000)
            body = await r.body()
            ct   = r.headers.get("content-type", "")
            print(f"  [{r.status}] CT={ct}  len={len(body)}")
            if body[:4] == b'%PDF':
                fname = f"downloads/ConoSur_test/{nro.replace(' ', '_')}.pdf"
                with open(fname, "wb") as f:
                    f.write(body)
                print(f"  → PDF guardado: {fname} ✓")
            elif len(body) < 300:
                print(f"  → Body: {body}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

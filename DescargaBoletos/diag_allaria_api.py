"""
diag_allaria_api.py — Diagnostica el API de BOLETOS de Allaria.

Captura:
  1. La respuesta de GetBoletos para una fecha con operaciones
  2. La URL de descarga del PDF al clickear un boleto

Uso:
    DISPLAY=:0 python3 diag_allaria_api.py [YYYY-MM-DD]
    (usa hoy si no se pasa fecha)
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

_PROFILE_DIR = Path("browser_profiles/allaria")
_TIMEOUT = 30_000
_BASE    = "https://allaria-ssl.allaria.com.ar"


async def main():
    load_dotenv(Path(__file__).parent / ".env")

    fecha = sys.argv[1] if len(sys.argv) > 1 else datetime.today().strftime("%Y-%m-%d")
    dt = datetime.strptime(fecha, "%Y-%m-%d")
    fd = dt.strftime("%Y-%m-%dT03:00:00.000Z")
    fh = (dt + timedelta(days=1)).strftime("%Y-%m-%dT03:00:00.000Z")
    api_url = f"{_BASE}/AllariaOnline/UniWA/api/GetBoletos?FD={fd}&FH={fh}&TF=-1"

    logger.info("Fecha: %s  →  FD=%s  FH=%s", fecha, fd, fh)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,
            executable_path="/usr/bin/google-chrome-stable",
            slow_mo=50,
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # ── 1. Login / verificar sesión ───────────────────────────────────────
        await page.goto("https://allaria.com.ar/Account/RedirectLogin",
                        wait_until="load", timeout=_TIMEOUT)
        await page.wait_for_timeout(3000)
        current = page.url
        logger.info("URL tras redirect: %s", current)

        if "AllariaOnline" not in current and "VBolsaNet" not in current:
            logger.error("No hay sesión activa — corré setup_allaria_profile.py primero")
            await ctx.close()
            return

        # ── 2. Interceptar todas las respuestas relevantes ────────────────────
        captured_boletos: list[dict] = []
        pdf_requests: list[str] = []

        async def on_response(resp):
            if "GetBoletos" in resp.url:
                logger.info("→ GetBoletos  status=%s  url=%s", resp.status, resp.url)
                try:
                    data = await resp.json()
                    logger.info("  items=%d", len(data) if isinstance(data, list) else -1)
                    if isinstance(data, list) and data:
                        logger.info("  PRIMER ITEM:\n%s", json.dumps(data[0], indent=4, ensure_ascii=False))
                        captured_boletos.extend(data)
                except Exception as e:
                    text = await resp.text()
                    logger.error("  Error parse JSON: %s  body=%s", e, text[:300])

        def on_request(req):
            if any(kw in req.url for kw in ("GetFormBol", "DownloadBol", "GetPDF", "boleto", "pdf")):
                logger.info("→ PDF request: %s %s", req.method, req.url)
                pdf_requests.append(req.url)

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── 3. Click en menú BOLETOS ──────────────────────────────────────────
        logger.info("Esperando que cargue el dashboard…")
        await page.wait_for_timeout(2000)

        # Dump elementos de menú para diagnóstico
        items = await page.evaluate("""
            () => [...document.querySelectorAll('md-item-content, md-list-item')]
                    .map(e => e.innerText?.trim())
                    .filter(Boolean)
                    .slice(0, 20)
        """)
        logger.info("Menú items: %s", items)

        try:
            boletos_btn = page.locator("md-item-content, md-list-item").filter(has_text="BOLETOS").first
            await boletos_btn.click(timeout=5000)
            logger.info("Click BOLETOS — OK")
        except Exception as e:
            logger.warning("No encontré botón BOLETOS vía locator: %s", e)
            # Fallback: buscar cualquier elemento con texto BOLETOS
            try:
                await page.locator("text=BOLETOS").first.click(timeout=5000)
                logger.info("Click BOLETOS (fallback) — OK")
            except Exception as e2:
                logger.error("No pude hacer click en BOLETOS: %s", e2)

        await page.wait_for_timeout(3000)
        logger.info("URL tras click BOLETOS: %s", page.url)

        # ── 4. Llamar GetBoletos API directamente ─────────────────────────────
        logger.info("Llamando GetBoletos API: %s", api_url)
        result = await page.evaluate(f"""
            async () => {{
                try {{
                    const r = await fetch('{api_url}', {{
                        credentials: 'include',
                        headers: {{'Accept': 'application/json'}}
                    }});
                    const text = await r.text();
                    return {{status: r.status, body: text}};
                }} catch(e) {{
                    return {{status: -1, body: e.toString()}};
                }}
            }}
        """)
        logger.info("GetBoletos  status=%s", result["status"])
        body = result["body"]
        logger.info("GetBoletos  body (primeros 2000 chars):\n%s", body[:2000])

        # Parsear si es JSON
        try:
            parsed = json.loads(body)
            if isinstance(parsed, list) and parsed:
                logger.info("PRIMER ITEM del API directo:\n%s",
                            json.dumps(parsed[0], indent=4, ensure_ascii=False))
                captured_boletos.extend(parsed)
        except Exception:
            pass

        # ── 5. Si hay boletos, intentar clickear uno para capturar URL de PDF ─
        if captured_boletos:
            logger.info("Total boletos capturados: %d", len(captured_boletos))
            logger.info("Intentando click en primer ícono PDF de la grilla…")
            await page.wait_for_timeout(2000)

            # Dump estructura DOM relevante
            dom = await page.evaluate("""
                () => ({
                    hash:      location.hash,
                    trDataId:  document.querySelectorAll('tr[data-id]').length,
                    pdfIcons:  document.querySelectorAll('a.icon-file-pdf, [class*="pdf"]').length,
                    igGrid:    document.querySelectorAll('[id*="Grid"], [class*="igGrid"]').length,
                    bodySnip:  document.body.innerText.slice(0, 800),
                })
            """)
            logger.info("DOM: %s", json.dumps(dom, indent=2, ensure_ascii=False))

            # Intentar click en ícono PDF
            try:
                pdf_icon = page.locator("a.icon-file-pdf.app_gridIcon, a.icon-file-pdf").first
                await pdf_icon.wait_for(state="visible", timeout=5000)
                await pdf_icon.click()
                await page.wait_for_timeout(2000)
                logger.info("Click PDF icon — OK")
                logger.info("PDF requests capturados: %s", pdf_requests)

                # Ver qué hay en el dialog
                dialog_html = await page.evaluate("""
                    () => document.querySelector('.md-dialog-container')?.innerHTML?.slice(0, 1000) || 'no dialog'
                """)
                logger.info("Dialog HTML:\n%s", dialog_html)
            except Exception as e:
                logger.warning("No pude clickear ícono PDF: %s", e)
        else:
            logger.warning("No se capturaron boletos — revisá la fecha o la sesión")

        input("\n>>> Presioná Enter para cerrar el browser...")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())

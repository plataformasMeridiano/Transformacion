"""
setup_allaria_profile.py — Renueva el perfil persistente de Allaria.

El script completa usuario y contraseña automáticamente. Si Auth0 pide
código 2FA, lo solicita por consola — no hay que tocar el browser.

Uso:
    python3 setup_allaria_profile.py
    → Ingresá el código 2FA cuando se pida.
    → Una vez en el dashboard, el browser se cierra solo y el perfil queda
      guardado en browser_profiles/allaria/ por ~30 días.
"""
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

_URL         = "https://allaria.com.ar/Account/RedirectLogin"
_PROFILE_DIR = Path("browser_profiles/allaria")
_TIMEOUT     = 30_000


async def main():
    load_dotenv(Path(__file__).parent / ".env")
    usuario   = os.environ["ALLARIA_USUARIO"]
    contrasena = os.environ["ALLARIA_PASSWORD"]

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Perfil: %s", _PROFILE_DIR)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,
            executable_path="/usr/bin/google-chrome-stable",
            slow_mo=80,
            viewport={"width": 1280, "height": 800},
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        logger.info("Navegando a %s …", _URL)
        await page.goto(_URL, wait_until="load", timeout=_TIMEOUT)
        await page.wait_for_timeout(3000)

        current_url = page.url
        logger.info("URL inicial: %s", current_url)

        # ── Caso 1: ya autenticado ────────────────────────────────────────────
        if "AllariaOnline" in current_url or "VBolsaNet" in current_url:
            logger.info("✓ Sesión activa — ya en VBolsaNet. No hace falta renovar.")
            await ctx.close()
            return

        # ── Caso 2: formulario Auth0 — completar credenciales ─────────────────
        logger.info("Formulario Auth0 detectado — completando credenciales…")
        await page.wait_for_load_state("networkidle", timeout=_TIMEOUT)

        username_input = page.locator("input#username, input[name='username']").first
        await username_input.wait_for(state="visible", timeout=_TIMEOUT)
        await username_input.fill(usuario)

        pwd_input = page.locator("input#password, input[name='password']").first
        await pwd_input.fill(contrasena)

        await page.locator("button[type='submit']:not(:has-text('Google'))").first.click()
        await page.wait_for_timeout(4000)

        # ── Caso 3: 2FA requerido ─────────────────────────────────────────────
        post_url = page.url
        page_text = await page.evaluate("document.body.innerText.slice(0, 1000)")

        if any(kw in page_text.lower() for kw in ("código", "verificación", "otp", "autenticador", "authenticator", "verify")):
            logger.info("─" * 50)
            logger.info("2FA requerido.")
            _CODE_FILE = Path("/tmp/allaria_code.txt")
            _CODE_FILE.unlink(missing_ok=True)
            logger.info(">>> Escribí el código en /tmp/allaria_code.txt y esperá...")
            while not _CODE_FILE.exists() or not _CODE_FILE.read_text().strip():
                await asyncio.sleep(1)
            codigo = _CODE_FILE.read_text().strip()
            _CODE_FILE.unlink(missing_ok=True)
            logger.info("Código recibido: %s", codigo)

            # Intentar ingresar el código en el campo visible
            otp_input = page.locator(
                "input[name='code'], input[autocomplete='one-time-code'], "
                "input[inputmode='numeric'], input[type='text'], input[type='number']"
            ).first
            await otp_input.wait_for(state="visible", timeout=_TIMEOUT)
            await otp_input.fill(codigo)

            submit_btn = page.locator("button[type='submit'], button[value='default']").first
            await submit_btn.click()
            await page.wait_for_timeout(5000)

        # ── Esperar redirect a VBolsaNet / AllariaOnline ──────────────────────
        logger.info("Esperando redirect a VBolsaNet…")
        try:
            await page.wait_for_url(
                lambda url: (
                    "AllariaOnline" in url
                    or "VBolsaNet" in url
                    or (url.startswith("https://allaria.com.ar") and "login" not in url.lower())
                ),
                timeout=_TIMEOUT * 2,
            )
        except Exception:
            final_url = page.url
            logger.error("No llegamos a VBolsaNet — URL final: %s", final_url)
            logger.error("Texto de página: %s", (await page.evaluate("document.body.innerText.slice(0,500)")))
            await ctx.close()
            return

        final_url = page.url
        logger.info("URL post-login: %s", final_url)

        # Si quedamos en allaria.com.ar (no en VBolsaNet), invocar getHomeEsco()
        if "AllariaOnline" not in final_url and "VBolsaNet" not in final_url:
            logger.info("Invocando getHomeEsco() para abrir VBolsaNet…")
            try:
                async with ctx.expect_page(timeout=_TIMEOUT) as new_page_info:
                    await page.evaluate("getHomeEsco()")
                new_page = await new_page_info.value
                await new_page.wait_for_url(
                    lambda url: "AllariaOnline" in url or "VBolsaNet" in url,
                    timeout=_TIMEOUT,
                )
                logger.info("VBolsaNet URL: %s", new_page.url)
            except Exception as e:
                logger.warning("getHomeEsco() no abrió nueva pestaña: %s", e)

        await page.wait_for_timeout(3000)
        logger.info("─" * 50)
        logger.info("✓ Login completado. Cerrando browser y guardando perfil…")
        await ctx.close()

    logger.info("✓ Perfil guardado en %s — válido por ~30 días.", _PROFILE_DIR)


if __name__ == "__main__":
    asyncio.run(main())

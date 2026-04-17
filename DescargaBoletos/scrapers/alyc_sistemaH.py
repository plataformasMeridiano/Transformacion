import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright

from .alyc_sistemaB import AdcapScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000
_PROFILE_DIR = Path("browser_profiles/allaria")

# Auth0 redirect URL (SSO entry point para Allaria)
_URL_REDIRECT = "https://allaria.com.ar/Account/RedirectLogin"

# Códigos de tipo de operación para Allaria
_ECHEQ_CODES  = frozenset({"VCHDIF", "VCHDCON"})          # Venta eCheq/Cheque Diferido
_PAGARE_CODES = frozenset({"VPAGSEC", "VPDIFCON", "VPAG"}) # Pagarés (placeholder)


class AllariaScraper(AdcapScraper):
    """
    Scraper para Allaria Online (sistemaH).

    Hereda toda la lógica de descarga de AdcapScraper (portal VBhome/Unisync).
    Override de login y contexto de browser para manejar:
        - Auth0 / SSO en lugar del formulario VBhome directo
        - Persistent context para preservar device trust de 2FA (30 días)

    Flujo de login:
        1. Navegar a https://allaria.com.ar/Account/RedirectLogin
        2. Si ya redirige a VBolsaNet (sesión Auth0 activa) → listo
        3. Si aparece formulario Auth0:
            a. Completar email → Continuar
            b. Completar contraseña → Ingresar
            c. Si pide 2FA → lanzar excepción (hay que renovar perfil)
        4. Esperar redirect a AllariaOnline/VBolsaNet

    Setup previo:
        python3 setup_allaria_profile.py
        → Completar login + 2FA manualmente.
        → El perfil queda guardado en browser_profiles/allaria/
        → Device trust válido por 30 días.
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self._persistent_context = None

    # ── Lifecycle: persistent context ─────────────────────────────────────────

    async def __aenter__(self):
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._persistent_context = await self._playwright.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=self.headless,
            executable_path="/usr/bin/google-chrome-stable",
            slow_mo=50,
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self._page = (
            self._persistent_context.pages[0] if self._persistent_context.pages
            else await self._persistent_context.new_page()
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._persistent_context:
            await self._persistent_context.close()
        if self._playwright:
            await self._playwright.stop()

    # ── Clasificación de tipo de operación ───────────────────────────────────

    def _classify_tipo(self, cells: list[str]) -> str:
        """
        Allaria usa códigos distintos a ADCAP:
          VCHDIF / VCHDCON → Venta FCE-eCheq
          APCOLCON / APCOLFUT (caucion_codes) → Cauciones
          otros → Pases
        """
        code = cells[4].strip().upper() if len(cells) > 4 else ""
        if code in _ECHEQ_CODES:
            return "Venta FCE-eCheq"
        if code in _PAGARE_CODES:
            return "Pagarés"
        if code in self._caucion_codes:
            return "Cauciones"
        return "Pases"

    # ── Login via Auth0 ───────────────────────────────────────────────────────

    async def login(self) -> bool:
        page = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        logger.info("[%s] Navegando a %s", self.nombre, _URL_REDIRECT)
        await page.goto(_URL_REDIRECT, wait_until="load", timeout=timeout)
        await page.wait_for_timeout(3000)

        current_url = page.url
        logger.info("[%s] URL tras redirect inicial: %s", self.nombre, current_url)

        # ── Caso 1: ya autenticado, estamos en VBolsaNet ──────────────────────
        if "AllariaOnline" in current_url or "VBolsaNet" in current_url:
            logger.info("[%s] Sesión activa — ya en VBolsaNet", self.nombre)
            return True

        # ── Caso 2: formulario Auth0 ──────────────────────────────────────────
        logger.info("[%s] Formulario Auth0 detectado — completando credenciales", self.nombre)

        # Esperar que la página termine de cargar completamente
        await page.wait_for_load_state("networkidle", timeout=timeout)

        # Auth0 muestra username + password en la misma página
        username_input = page.locator("input#username, input[name='username']").first
        await username_input.wait_for(state="visible", timeout=timeout)
        await username_input.fill(self.usuario)

        pwd_input = page.locator("input#password, input[name='password']").first
        await pwd_input.fill(self.contrasena)

        # "Iniciar sesión"
        await page.locator("button[type='submit']:not(:has-text('Google'))").first.click()

        await page.wait_for_timeout(4000)

        # ── Verificar si pide 2FA ─────────────────────────────────────────────
        post_url = page.url
        if "allaria.com.ar" in post_url and "AllariaOnline" not in post_url and "VBolsaNet" not in post_url:
            page_text = await page.evaluate("document.body.innerText.slice(0, 500)")
            if any(kw in page_text.lower() for kw in ("código", "verificación", "otp", "autenticador", "authenticator")):
                raise RuntimeError(
                    f"[{self.nombre}] 2FA requerido — ejecutar setup_allaria_profile.py "
                    "para renovar la sesión del perfil."
                )

        # ── Esperar a que Auth0 complete el redirect (a allaria.com.ar o a VBolsaNet) ──
        try:
            await page.wait_for_url(
                lambda url: (
                    "AllariaOnline" in url
                    or "VBolsaNet" in url
                    or (url.startswith("https://allaria.com.ar") and "login" not in url)
                ),
                timeout=timeout,
            )
        except Exception:
            final_url = page.url
            raise RuntimeError(
                f"[{self.nombre}] Login Auth0 no completó — URL final: {final_url}"
            )

        post_login_url = page.url
        logger.info("[%s] Auth0 completado — URL: %s", self.nombre, post_login_url)

        # ── Si quedamos en allaria.com.ar, invocar getHomeEsco() para ir a VBolsaNet ──
        if "AllariaOnline" not in post_login_url and "VBolsaNet" not in post_login_url:
            logger.info("[%s] Invocando getHomeEsco() para ir a VBolsaNet...", self.nombre)
            # getHomeEsco() puede abrir una nueva pestaña o navegar en la misma
            async with self._persistent_context.expect_page(timeout=timeout) as new_page_info:
                await page.evaluate("getHomeEsco()")
            new_page = await new_page_info.value
            # Esperar que la URL de VBolsaNet se establezca (SPA con hash routing)
            await new_page.wait_for_url(
                lambda url: "AllariaOnline" in url or "VBolsaNet" in url,
                timeout=timeout,
            )
            self._page = new_page
            page = new_page
            await page.wait_for_timeout(5000)
            logger.info("[%s] VBolsaNet URL: %s", self.nombre, page.url)

        logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
        return True

    # ── Filtro de fecha: selecciona "Por concertación" antes de filtrar ───────

    async def _aplicar_filtro_fecha(self, fecha_iso: str) -> None:
        """
        Override para el portal Allaria: el dialog de filtro incluye un radio
        "Tipo de fecha" que debe setearse a 'Por concertación' (value='-1').
        Luego aplica la misma lógica de scope Angular que AdcapScraper.
        """
        page = self._page
        # Abrir el diálogo de filtro
        await page.evaluate(
            "() => { const ic = document.querySelector('span.icon-filter'); if (ic) ic.click(); }"
        )
        await asyncio.sleep(1.5)

        result = await page.evaluate(f"""
            () => {{
                const dialog = document.querySelector('.md-dialog-container');
                if (!dialog) return 'no-dialog';
                const scope = angular.element(dialog).scope();
                if (!scope?.filters) return 'no-filters';

                // Forzar "Por concertación" (tipoFecha = -1, ya es el default en Allaria)
                scope.filters.tipoFecha = -1;

                const d = new Date('{fecha_iso}T12:00:00-03:00');
                scope.filters.fechaDesde = d;
                scope.filters.fechaHasta = d;
                scope.$apply();
                scope.filter();
                return 'ok';
            }}
        """)
        logger.debug("[%s] Filtro fecha %s: %s", self.nombre, fecha_iso, result)
        await asyncio.sleep(3)

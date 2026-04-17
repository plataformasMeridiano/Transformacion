import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
import base64

from playwright.async_api import async_playwright
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000
_GQL_URL       = "https://home.max.capital/backend/api/graphql"
_DOWNLOAD_BASE = "https://home.max.capital/backend/api/v1/files/receipts/pdf"
_HOME_URL      = "https://home.max.capital/"

# Códigos de operación que identifican cauciones en el campo "detail"
# detail = "Boleto / 37087 / APTOMCONC / 0 / $"
_DEFAULT_CAUCION_CODES = frozenset({"APTOMCONC", "APTOMFUTC"})


class MaxCapitalScraper(BaseScraper):
    """
    Scraper para Max Capital (Next.js + Keycloak SSO + GraphQL API).

    Flujo de login:
        1. Navegar a home.max.capital — redirige a sso.max.capital (Keycloak)
        2. Esperar #usernameLoginWeb y completar credenciales
        3. Submit → wait_for_url sin sso.max.capital
        4. Retornar True (la pantalla "Select account" queda pendiente)
        NOTA: la selección de cuenta se hace en download_tickets, una vez por cuenta.

    Flujo de descarga (download_tickets):
        Por cada cuenta configurada en opciones.cuentas:
        1. Navegar a home.max.capital para ver la pantalla "Select account"
        2. Esperar radio buttons (input[type='radio'])
        3. Seleccionar el radio que contenga cuenta.numero en su texto
        4. Click en "Continue"
        5. Navegar a /en/account/movements/monetary?from=FECHA&to=FECHA&select=OPERATION
        6. Capturar respuesta GQL getCurrencyTransactionsAccount
        7. Descargar PDFs via fetch() del browser (evita bloqueo Cloudflare)
        8. Guardar en dest_dir/{cuenta.nombre}/{tipo}/ si hay múltiples cuentas

    Configuración relevante en opciones:
        cuentas        (list[dict])  Lista de cuentas: {"nombre": str, "numero": int}
                                     numero debe matchear el número visible en el radio button.
                                     Si está vacío o ausente: selecciona el primer radio (compat).
        caucion_codes  (list[str])   Códigos en el campo "detail" que identifican cauciones.
                                     Default: ["APTOMCONC", "APTOMFUTC"].
        tipo_operacion (list[str])   Subtipos a descargar: "Cauciones", "Pases".
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        codes = self.opciones.get("caucion_codes")
        self._caucion_codes = (
            frozenset(c.upper() for c in codes)
            if codes is not None
            else _DEFAULT_CAUCION_CODES
        )
        self._auth_token: str | None = None
        self._gql_movements: dict | None = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await self._browser.new_context(accept_downloads=True)
        self._page = await context.new_page()
        return self

    def _classify_tipo(self, detail: str) -> str:
        """Clasifica un boleto como 'Cauciones' o 'Pases' según el código del detail.

        detail format: "Boleto / 37087 / APTOMCONC / 0 / $"
        """
        parts = [p.strip().upper() for p in detail.split("/")]
        for part in parts:
            if part in self._caucion_codes:
                return "Cauciones"
        return "Pases"

    def _setup_interceptors(self, page):
        """Registra los interceptores de requests/responses para capturar token y GQL."""

        async def on_request(req):
            hdrs = dict(req.headers)
            if "authorization" in hdrs and hdrs["authorization"].startswith("Bearer"):
                self._auth_token = hdrs["authorization"]

        async def on_response(resp):
            if "graphql" in resp.url:
                try:
                    req_body = resp.request.post_data or ""
                    parsed = json.loads(req_body) if req_body else {}
                    if parsed.get("operationName") == "getCurrencyTransactionsAccount":
                        self._gql_movements = await resp.json()
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

    async def login(self) -> bool:
        page = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        # Registrar interceptores desde el inicio para capturar Bearer token
        self._setup_interceptors(page)

        logger.info("[%s] Navegando a %s", self.nombre, self.url_login)
        await page.goto(self.url_login, wait_until="domcontentloaded", timeout=timeout)

        # Esperar formulario Keycloak (el JS redirect puede tardar)
        logger.info("[%s] Esperando formulario Keycloak", self.nombre)
        await page.wait_for_selector("#usernameLoginWeb", timeout=timeout)
        logger.info("[%s] Keycloak URL: %s", self.nombre, page.url)

        await page.fill("#usernameLoginWeb", self.usuario)
        await page.fill("#passwordLoginWeb", self.contrasena)
        await page.click("input[type='submit'], button[type='submit']")
        await page.wait_for_url(lambda u: "sso.max.capital" not in u, timeout=timeout)
        await page.wait_for_timeout(2000)

        # La pantalla "Select account" queda pendiente — la resuelve download_tickets.
        logger.info("[%s] SSO completado — URL: %s", self.nombre, page.url)
        return True

    async def _select_account(self, numero: int | None, timeout: int) -> bool:
        """
        Navega a home.max.capital, espera la pantalla de selección de cuenta,
        selecciona el radio que contiene `numero` en su texto (o el primero si
        `numero` es None), y hace click en Continue.

        Retorna True si la selección fue exitosa, False si no aparecen radios.
        """
        page = self._page
        await page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(2000)

        # Esperar la pantalla de selección de cuenta
        try:
            await page.wait_for_selector("input[type='radio']", timeout=10_000)
        except Exception:
            logger.warning("[%s] Pantalla de selección de cuenta no apareció", self.nombre)
            return False

        if numero is not None:
            # Buscar el radio cuyo label contiene el número de cuenta
            radios = page.locator("input[type='radio']")
            count  = await radios.count()
            selected = False
            for idx in range(count):
                radio   = radios.nth(idx)
                # El label está en el párrafo/texto cercano al radio
                parent  = radio.locator("xpath=..")
                txt     = await parent.inner_text()
                if str(numero) in txt:
                    await radio.click(force=True)
                    selected = True
                    logger.info("[%s] Radio seleccionado: %s", self.nombre, txt.strip()[:60])
                    break
            if not selected:
                logger.warning(
                    "[%s] No se encontró radio para número %s — usando el primero",
                    self.nombre, numero,
                )
                await radios.first.click(force=True)
        else:
            await page.locator("input[type='radio']").first.click(force=True)

        await page.wait_for_timeout(500)
        await page.locator("button", has_text="Continue").click(force=True)
        await page.wait_for_timeout(3000)
        return True

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga los boletos de la fecha indicada (YYYY-MM-DD).

        Con múltiples cuentas configuradas, la estructura de destino es:
            dest_dir/
            ├── MeridianoNorte/
            │   ├── Cauciones/
            │   └── Pases/
            └── Pamat/
                ├── Cauciones/
                └── Pases/

        Con una sola cuenta (o sin `cuentas` en opciones), estructura plana.
        """
        page         = self._page
        timeout      = self.opciones.get("timeout_ms", _TIMEOUT)
        tipos_config: list[str] = self.opciones.get("tipo_operacion", [])
        cuentas: list[dict]     = self.opciones.get("cuentas", [{}])
        fecha_dt  = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_iso = fecha_dt.strftime("%Y-%m-%d")

        multi = len(cuentas) > 1 and any(c.get("nombre") for c in cuentas)

        downloaded: list[Path] = []

        for cuenta in cuentas:
            numero     = cuenta.get("numero")       # int o None
            cta_nombre = cuenta.get("nombre", "")

            # ── 1. Seleccionar cuenta ─────────────────────────────────────────
            logger.info("[%s] Seleccionando cuenta %s (%s)",
                        self.nombre, numero or "primera", cta_nombre or "default")
            ok = await self._select_account(numero, timeout)
            if not ok:
                logger.error("[%s] No se pudo seleccionar cuenta %s — omitiendo",
                             self.nombre, cta_nombre or "primera")
                continue

            dest_base = dest_dir / cta_nombre if (multi and cta_nombre) else dest_dir

            # ── 2. Navegar a movimientos monetarios ───────────────────────────
            self._gql_movements = None
            url = (
                f"https://home.max.capital/en/account/movements/monetary"
                f"?from={fecha_iso}&to={fecha_iso}&select=OPERATION"
            )
            logger.info("[%s] Navegando a movimientos %s [%s]",
                        self.nombre, fecha, cta_nombre or "default")
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            # Esperar a que el GQL call se dispare y se capture
            for _ in range(20):
                await page.wait_for_timeout(500)
                if self._gql_movements is not None:
                    break

            if self._gql_movements is None:
                logger.warning("[%s] GQL de movimientos no capturado para %s [%s]",
                               self.nombre, fecha, cta_nombre or "default")
                continue

            # ── 3. Filtrar boletos descargables ───────────────────────────────
            movs    = self._gql_movements.get("data", {}).get("currentAccountsMonetary", [])
            boletos = [m for m in movs if m.get("downloadId")]
            logger.info("[%s] Boletos descargables el %s [%s]: %d",
                        self.nombre, fecha, cta_nombre or "default", len(boletos))

            if not boletos:
                continue

            if not self._auth_token:
                logger.error("[%s] Bearer token no capturado", self.nombre)
                continue

            # ── 4. Descargar PDFs ─────────────────────────────────────────────
            for m in boletos:
                detail = m.get("detail", "")
                did    = m["downloadId"]
                tipo   = self._classify_tipo(detail)

                if tipo not in tipos_config:
                    logger.debug("[%s] Omitiendo %s (tipo '%s' no configurado)",
                                 self.nombre, detail, tipo)
                    continue

                dest_tipo_dir = dest_base / tipo
                dest_tipo_dir.mkdir(parents=True, exist_ok=True)

                # Extraer número de boleto del detail ("Boleto / 37087 / ...")
                parts = [p.strip() for p in detail.split("/")]
                nro   = parts[1] if len(parts) > 1 else str(m["id"])

                dl_url = f"{_DOWNLOAD_BASE}?downloadId={quote(did, safe='')}"
                logger.info("[%s] Descargando %s → tipo=%s [%s]",
                            self.nombre, detail, tipo, cta_nombre or "default")

                try:
                    # Descargar via fetch del browser (necesario para pasar Cloudflare)
                    result = await page.evaluate(
                        """async ([url, token]) => {
                            try {
                                const r = await fetch(url, {
                                    headers: { 'Authorization': token },
                                    credentials: 'include'
                                });
                                if (!r.ok) return { ok: false, status: r.status };
                                const buf = await r.arrayBuffer();
                                const bytes = new Uint8Array(buf);
                                let binary = '';
                                for (let i = 0; i < bytes.byteLength; i++)
                                    binary += String.fromCharCode(bytes[i]);
                                return { ok: true, status: r.status, b64: btoa(binary), len: buf.byteLength };
                            } catch(e) {
                                return { ok: false, error: e.toString() };
                            }
                        }""",
                        [dl_url, self._auth_token],
                    )

                    if not result.get("ok"):
                        logger.error("[%s] Descarga fallida — %s", self.nombre, result)
                        continue

                    body = base64.b64decode(result["b64"])
                    if body[:4] != b"%PDF":
                        logger.warning("[%s] Respuesta no es PDF para %s", self.nombre, detail)
                        continue

                    fname = dest_tipo_dir / f"BOLETO_{nro}.pdf"
                    fname.write_bytes(body)
                    logger.info("[%s] Guardado: %s (%d bytes)",
                                self.nombre, fname.name, len(body))
                    downloaded.append(fname)

                except Exception as exc:
                    logger.error("[%s] Error descargando %s — %s: %s",
                                 self.nombre, detail, type(exc).__name__, exc)

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

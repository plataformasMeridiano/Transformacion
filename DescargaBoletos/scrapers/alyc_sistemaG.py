import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT      = 60_000

# Defaults de plataforma Fermi (= Dhalmore). Otras ALYCs sobre Fermi (ej. WIN)
# sobreescriben api_base/url_base/profile_dir/device_id vía opciones en config.
_API_BASE     = "https://core.dhalmore.prod.fermi.galloestudio.com/api"
_URL_BASE     = "https://clientes.dhalmorecap.com/"
_PROFILE_DIR  = Path("browser_profiles/dhalmore")
_DEVICE_ID    = "49dde3e5-bae6-4067-9930-5f213a2468a8"

# Tipo de reporte en la API
_TIPO_API = {
    "Cauciones": "CAUCION",
    "Pases":     "PASS",
}

# Operaciones que no son boletos de trading (se filtran de la descarga)
_OP_SKIP = frozenset({"DRIG"})


class DhalmoreScraper(BaseScraper):
    """
    Scraper para plataforma Fermi de Gallo Estudio (Dhalmore Capital, WIN Securities).

    El mismo backend Fermi (`core.<alyc>.prod.fermi.galloestudio.com`) sirve a
    varias ALYCs; este scraper es config-driven vía opciones para reusarse entre
    ellas (defaults = Dhalmore).

    Flujo:
      1. Login con perfil persistente (browser_profiles/<alyc>/) — evita MFA
         después del primer uso. Si pide MFA escribe el código en
         /tmp/<alyc>_code.txt (Auth0 device verification).
      2. Captura bearer token via interceptor de requests outgoing.
      3. Descarga lista de movimientos via httpx (no page.evaluate) con el bearer.
      4. Descarga cada boleto PDF.

    Config relevante en opciones:
      cuentas        list[dict]  {"nombre": str, "customer_account_id": int}
      tipo_operacion list[str]   "Cauciones" y/o "Pases"
      api_base       str         Base de la API Fermi (default: Dhalmore)
      url_base       str         URL del portal (default: Dhalmore)
      profile_dir    str         Perfil persistente Chrome (default: browser_profiles/dhalmore)
      device_id      str         Device ID verificado (default: hardcoded Dhalmore)
      client_name    str         Header x-client-name (default: "WEB 0.38.2")
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self._bearer: str | None = None
        self._context = None   # persistent context (no _browser)
        # Parámetros de plataforma Fermi (config-driven; defaults = Dhalmore)
        self._api_base    = self.opciones.get("api_base", _API_BASE)
        self._url_base    = self.opciones.get("url_base", _URL_BASE)
        self._profile_dir = Path(self.opciones.get("profile_dir", str(_PROFILE_DIR)))
        self._device_id   = self.opciones.get("device_id", _DEVICE_ID)
        self._client_name = self.opciones.get("client_name", "WEB 0.38.2")

    # ── Lifecycle: persistent context ────────────────────────────────────────

    async def __aenter__(self):
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self._profile_dir),
            headless=self.headless,
            slow_mo=50,
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        self._page = (
            self._context.pages[0] if self._context.pages
            else await self._context.new_page()
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    # ── Interceptores ────────────────────────────────────────────────────────

    def _setup_interceptors(self):
        async def on_request(req):
            if req.resource_type in ("xhr", "fetch") and "fermi" in req.url:
                auth = req.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    self._bearer = auth

        async def on_response(resp):
            if "/oauth/token" in resp.url:
                try:
                    j = json.loads(await resp.body())
                    if "access_token" in j:
                        self._bearer = f"Bearer {j['access_token']}"
                except Exception:
                    pass

        self._page.on("request",  on_request)
        self._page.on("response", on_response)

    # ── Login ────────────────────────────────────────────────────────────────

    async def login(self, timeout: int = _TIMEOUT) -> bool:
        self._setup_interceptors()

        logger.info("[%s] Cargando app...", self.nombre)
        await self._page.goto(self._url_base, wait_until="networkidle", timeout=timeout)
        await self._page.wait_for_timeout(3000)

        if "auth0" in self._page.url:
            logger.info("[%s] Haciendo login con %s", self.nombre, self.usuario)
            await self._page.wait_for_selector("input[name='username']", timeout=15_000)
            await self._page.fill("input[name='username']", self.usuario)
            await self._page.fill("input[name='password']", self.contrasena)
            await self._page.click("button[type='submit']")
            await self._page.wait_for_timeout(4000)

        # MFA (device verification) — se pide solo en dispositivo nuevo
        code_input = self._page.locator(
            "input[placeholder*='código' i], input[placeholder*='code' i]"
        ).first
        if await code_input.count() > 0:
            code_file = Path(f"/tmp/{self.nombre.lower()}_code.txt")
            wait_file = Path(f"/tmp/{self.nombre.lower()}_waiting.txt")
            logger.warning("[%s] ⚠️  Device verification requerida!", self.nombre)
            logger.warning("[%s]    Escribir código en %s", self.nombre, code_file)
            wait_file.write_text("waiting")
            import time
            deadline = time.time() + 600
            while time.time() < deadline:
                if code_file.exists():
                    code = code_file.read_text().strip()
                    code_file.unlink(missing_ok=True)
                    wait_file.unlink(missing_ok=True)
                    await code_input.fill(code)
                    await self._page.click("button:has-text('Continuar')")
                    await self._page.wait_for_timeout(3000)
                    confirm = self._page.locator("button:has-text('Continuar')").first
                    if await confirm.count() > 0:
                        await confirm.click()
                        await self._page.wait_for_timeout(3000)
                    break
                await asyncio.sleep(1)
        else:
            logger.info("[%s] Sin MFA — dispositivo conocido ✓", self.nombre)

        # Capturar el bearer: la SPA lo emite en su primer request autenticado a
        # la API Fermi (o al refrescar el token vía /oauth/token). El tiempo hasta
        # ese primer request es variable, así que hacemos polling y, si tarda,
        # forzamos un reload que re-dispara las llamadas autenticadas del dashboard.
        import time
        for intento in range(4):
            t0 = time.time()
            while time.time() - t0 < 8:
                if self._bearer:
                    break
                await self._page.wait_for_timeout(500)
            if self._bearer:
                break
            logger.info("[%s] Bearer aún no capturado (intento %d/4) — recargando app",
                        self.nombre, intento + 1)
            try:
                await self._page.reload(wait_until="networkidle", timeout=timeout)
            except Exception:
                pass

        if not self._bearer:
            logger.error("[%s] No se capturó bearer token", self.nombre)
            return False

        logger.info("[%s] Login OK ✓", self.nombre)
        return True

    # ── API helpers (httpx) ──────────────────────────────────────────────────

    def _build_headers(self) -> dict:
        return {
            "Authorization": self._bearer,
            "Accept":        "application/json, text/plain, */*",
            "x-device-id":                 self._device_id,
            "x-use-wrapped-single-values": "true",
            "x-client-name":               self._client_name,
            "Origin":                      self._url_base.rstrip("/"),
            "Referer":                     self._url_base,
        }

    async def _fetch_movements(
        self,
        customer_account_id: int,
        tipo_api: str,
        currency: str,
        from_date: str,   # YYYY-MM-DD
        to_date: str,     # YYYY-MM-DD
    ) -> list[dict]:
        """
        GET /checking-accounts/customer-account/{id}/historical-movements
            ?currency={currency}&type={tipo}&fromDate={ISO}&toDate={ISO}
        """
        url = f"{self._api_base}/checking-accounts/customer-account/{customer_account_id}/historical-movements"
        params = {
            "currency": currency,
            "type":     tipo_api,
            "fromDate": f"{from_date}T00:00:00.000Z",
            "toDate":   f"{to_date}T00:00:00.000Z",
        }
        logger.debug("[%s] GET historical-movements %s %s %s→%s",
                     self.nombre, customer_account_id, tipo_api, from_date, to_date)
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.get(url, headers=self._build_headers(), params=params)
        if r.status_code != 200:
            logger.warning("[%s] historical-movements → %d: %s",
                           self.nombre, r.status_code, r.text[:300])
            return []
        data = r.json()
        content = data.get("content", data) if isinstance(data, dict) else data
        return content if isinstance(content, list) else []

    async def _fetch_fce_movements(
        self,
        customer_account_id: int,
        fecha: str,
    ) -> list[dict]:
        """
        GET /checking-accounts/customer-account/{id}/historical-instrument-equities
            ?fromDate={fecha}T00:00:00.000Z&toDate={fecha}T00:00:00.000Z

        Devuelve operaciones de venta de FCE/eCheq/Pagarés (operation=VCHV)
        para la fecha de operación indicada.
        """
        url = f"{self._api_base}/checking-accounts/customer-account/{customer_account_id}/historical-instrument-equities"
        params = {
            "fromDate": f"{fecha}T00:00:00.000Z",
            "toDate":   f"{fecha}T00:00:00.000Z",
        }
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.get(url, headers=self._build_headers(), params=params)
        if r.status_code != 200:
            logger.warning("[%s] historical-instrument-equities → %d: %s",
                           self.nombre, r.status_code, r.text[:300])
            return []
        data = r.json()
        movs = data if isinstance(data, list) else data.get("content", [])
        return [
            m for m in movs
            if m.get("operationDate") == fecha
            and m.get("operation", "").upper() == "VCHV"
        ]

    async def _download_pdf(
        self,
        customer_account_id: int,
        document_key: str,
        order_code: str,
        receipt_code: int,
    ) -> bytes | None:
        """
        GET /checking-accounts/customer-account/{id}/ticket/{documentKey}
            ?orderCode={orderCode}&receiptCode={receiptCode}
        """
        url = (
            f"{self._api_base}/checking-accounts/customer-account/{customer_account_id}"
            f"/ticket/{document_key}"
        )
        params = {"orderCode": order_code, "receiptCode": receipt_code}
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.get(url, headers=self._build_headers(), params=params)
        if r.status_code != 200:
            logger.warning("[%s] ticket/%s → %d", self.nombre, document_key, r.status_code)
            return None
        if not r.content.startswith(b"%PDF"):
            logger.warning("[%s] ticket/%s no es PDF (%d bytes)",
                           self.nombre, document_key, len(r.content))
            return None
        return r.content

    # ── Download tickets ─────────────────────────────────────────────────────

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga boletos de una fecha (YYYY-MM-DD).

        Para cada cuenta configurada en opciones.cuentas y cada tipo en tipo_operacion:
          - Consulta historical-movements (ARS y USD)
          - Filtra por operationDate == fecha
          - Descarga PDF de cada movimiento
        """
        tipos_config = self.opciones.get("tipo_operacion", ["Cauciones", "Pases"])
        cuentas      = self.opciones.get("cuentas", [{}])
        multi        = len(cuentas) > 1 and any(c.get("nombre") for c in cuentas)

        # to_date = fecha + 60 días
        # historical-movements filtra por fecha de LIQUIDACIÓN, no concertación.
        # Para cauciones a plazo, el boleto TERMINO liquida semanas después de la
        # concertación. Con +60 días cubrimos todas las cauciones posibles.
        # Luego filtramos por operationDate == fecha (= fecha de concertación).
        from_dt  = datetime.strptime(fecha, "%Y-%m-%d")
        to_dt    = from_dt + timedelta(days=60)
        to_date  = to_dt.strftime("%Y-%m-%d")

        paths_descargados: list[Path] = []

        for cuenta in cuentas:
            cid    = cuenta.get("customer_account_id")
            cnombre = cuenta.get("nombre", "")
            if not cid:
                logger.warning("[%s] Cuenta '%s' sin customer_account_id — omitiendo",
                               self.nombre, cnombre or "?")
                continue

            for tipo_op in tipos_config:
                if tipo_op == "Venta FCE-eCheq":
                    continue  # se descarga por separado con _fetch_fce_movements
                if tipo_op == "Títulos":
                    logger.info("[%s] Tipo 'Títulos': tipo API de Dhalmore pendiente de identificar — omitiendo", self.nombre)
                    continue
                tipo_api = _TIPO_API.get(tipo_op)
                if not tipo_api:
                    logger.warning("[%s] Tipo desconocido: %s", self.nombre, tipo_op)
                    continue

                # Determinar directorio de destino
                if multi and cnombre:
                    tipo_dir = dest_dir / cnombre / tipo_op
                else:
                    tipo_dir = dest_dir / tipo_op
                tipo_dir.mkdir(parents=True, exist_ok=True)

                # Consultar ARS y USD
                movimientos: list[dict] = []
                for currency in ("ARS", "USD"):
                    movs = await self._fetch_movements(cid, tipo_api, currency, fecha, to_date)
                    # Filtrar por operationDate exacto
                    movs_dia = [m for m in movs if m.get("operationDate") == fecha]
                    logger.info("[%s] %s/%s/%s/%s → %d movimientos",
                                self.nombre, cnombre or cid, tipo_op, currency, fecha, len(movs_dia))
                    movimientos.extend(movs_dia)

                # Deduplicar por documentKey (por si ARS y USD duplican)
                seen_keys: set[str] = set()
                for mov in movimientos:
                    doc_key      = mov.get("documentKey", "")
                    order_code   = str(mov.get("orderCode", ""))
                    receipt_code = int(mov.get("receiptCode", 0))
                    currency_mov = mov.get("currency", "ARS")
                    description  = mov.get("description", "")

                    if not doc_key or doc_key in seen_keys:
                        continue
                    if mov.get("operation", "").upper() in _OP_SKIP:
                        logger.debug("[%s] Skip %s (%s)", self.nombre, order_code, mov.get("operation"))
                        continue
                    seen_keys.add(doc_key)

                    # Nombre de archivo: Boleto - {ALYC} - {orderCode}.pdf
                    fname = tipo_dir / f"Boleto - {self.nombre} - {order_code}.pdf"
                    if fname.exists():
                        logger.info("[%s] Skip (ya existe): %s", self.nombre, fname.name)
                        paths_descargados.append(fname)
                        continue

                    logger.info("[%s] Descargando %s (%s %s)…",
                                self.nombre, order_code, description, currency_mov)
                    try:
                        pdf_bytes = await self._download_pdf(cid, doc_key, order_code, receipt_code)
                    except Exception as exc:
                        logger.error("[%s] Timeout/error descargando %s: %s",
                                     self.nombre, order_code, exc)
                        continue
                    if pdf_bytes:
                        fname.write_bytes(pdf_bytes)
                        logger.info("[%s] Guardado: %s (%d KB)",
                                    self.nombre, fname.name, len(pdf_bytes) // 1024)
                        paths_descargados.append(fname)
                    else:
                        logger.error("[%s] Falló descarga: %s / %s", self.nombre, tipo_op, order_code)

        # ── FCE / eCheq / Pagarés (Venta Cheques MAV) ────────────────────────
        # Se consulta historical-instrument-equities para cada cuenta.
        # Los boletos van a dest_dir / "Venta FCE-eCheq" / archivo.pdf,
        # lo que hace que DriveUploader los suba al folder raíz de FCE.
        if "Venta FCE-eCheq" not in tipos_config:
            return paths_descargados

        fce_dir = dest_dir / "Venta FCE-eCheq"
        fce_dir.mkdir(parents=True, exist_ok=True)

        seen_fce: set[str] = set()
        for cuenta in cuentas:
            cid     = cuenta.get("customer_account_id")
            cnombre = cuenta.get("nombre", "")
            if not cid:
                continue
            fce_movs = await self._fetch_fce_movements(cid, fecha)
            logger.info("[%s] %s/FCE/%s → %d operaciones VCHV",
                        self.nombre, cnombre or cid, fecha, len(fce_movs))

            for mov in fce_movs:
                doc_key      = mov.get("documentKey", "")
                order_code   = str(mov.get("orderCode", ""))
                receipt_code = int(mov.get("receiptCode", 0))

                if not doc_key or doc_key in seen_fce:
                    continue
                seen_fce.add(doc_key)

                fname = fce_dir / f"Boleto - {self.nombre} - {order_code}.pdf"
                if fname.exists():
                    logger.info("[%s] Skip FCE (ya existe): %s", self.nombre, fname.name)
                    paths_descargados.append(fname)
                    continue

                logger.info("[%s] Descargando FCE %s (%s)…",
                            self.nombre, order_code, mov.get("description", "").strip())
                try:
                    pdf_bytes = await self._download_pdf(cid, doc_key, order_code, receipt_code)
                except Exception as exc:
                    logger.error("[%s] Timeout/error descargando FCE %s: %s",
                                 self.nombre, order_code, exc)
                    continue
                if pdf_bytes:
                    fname.write_bytes(pdf_bytes)
                    logger.info("[%s] Guardado FCE: %s (%d KB)",
                                self.nombre, fname.name, len(pdf_bytes) // 1024)
                    paths_descargados.append(fname)
                else:
                    logger.error("[%s] Falló descarga FCE: %s", self.nombre, order_code)

        return paths_descargados

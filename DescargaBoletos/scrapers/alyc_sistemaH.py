import io
import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import pyotp
from playwright.async_api import async_playwright

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT      = 30_000
_PROFILE_DIR  = Path("browser_profiles/allaria")

# Entry point del SSO. Era https://allaria.com.ar/Account/RedirectLogin (el portal
# viejo), que el 2026-08-20 empezó a devolver 404 y a renderizar la home
# institucional: la página nunca redirigía y el login moría esperando. El entry
# point real es la app, que redirige sola a login.allaria.com.ar/u/login.
# Se toma de url_login del config; esto es solo el fallback.
_URL_REDIRECT = "https://app.allaria.com.ar/"
_URL_APP      = "https://app.allaria.com.ar"
_API          = "https://api.allaria.cloud"

# market_operation_type → tipo de operación nuestro.
# Se clasifica por ESTE campo y no por metadata.operationTypeId: ese último falta
# en el 84% de los registros (de 228 ventas de cheque, 143 no lo traen), así que
# usarlo perdería la mayoría de los boletos.
_TIPOS_POR_OPERACION = {
    "VENTA_CHEQ":                 "Venta FCE-eCheq",
    "APERTURA_COLOCADOR_CONTADO": "Cauciones Colocadoras",
    "APERTURA_COLOCADOR_FUTURO":  "Cauciones Colocadoras",
}

# Operaciones rechazadas: no tienen boleto que valga la pena archivar.
_ESTADOS_EXCLUIDOS = frozenset({"REJECTED"})

# El número de boleto no viene en el JSON (ticket_id siempre null, y metadata.id
# es un id interno distinto). Está impreso en el PDF: "BOLETO\n#676265".
_RE_BOLETO = re.compile(r"BOLETO\s*#\s*(\d+)", re.I)

_VENTANA_DIAS_DEFAULT = 45

# El SSO pasa por tres URLs y distinguirlas por substring es traicionero: el punto
# de entrada es .../Account/RedirectLogin, que en minúsculas CONTIENE "login".
# Preguntar `"login" in url` daba verdadero en el entry point y falso con la L
# mayúscula, así que el scraper se salteaba el formulario y después se quedaba sin
# token. Se decide con chequeos explícitos y siempre en minúsculas.
_ENTRY_PATH = "/account/redirectlogin"


def _en_login(url: str) -> bool:
    """Estamos parados en el formulario de Auth0 (no en el punto de entrada)."""
    u = url.lower()
    return (("/login" in u) or ("login.allaria" in u)) and _ENTRY_PATH not in u


def _en_app(url: str) -> bool:
    """Llegamos a la aplicación ya autenticados."""
    u = url.lower()
    return "app.allaria.com.ar" in u and not _en_login(url)


class AllariaScraper(BaseScraper):
    """
    Scraper para Allaria (sistemaH) — plataforma nueva (app.allaria.com.ar).

    Allaria abandonó VBolsaNet en mayo de 2026 y migró a un SPA propio con API
    REST en api.allaria.cloud. Este scraper ya NO hereda de AdcapScraper: el
    portal VBhome del que se heredaba la descarga no existe más.

    Flujo:
        1. Login Auth0 (usuario + contraseña + TOTP) — sin cambios respecto del
           portal viejo; el redirect ahora termina en app.allaria.com.ar.
        2. Navegar a /actividad para que el SPA pida un token contra la API.
        3. Listar movimientos y bajar el PDF de cada uno por su ticket.

    ┌─ LIMITACIÓN CONOCIDA — SOLUCIÓN TEMPORAL ────────────────────────────────┐
    │                                                                          │
    │ La API NO permite consultar por fecha de concertación.                   │
    │                                                                          │
    │   • `from-date`/`to-date` de /by-mas/movements filtran por LIQUIDACIÓN.  │
    │     Verificado: una Apertura Colocadora concertada el 16/03 que liquida  │
    │     el 17/03 NO aparece en la ventana del 16 y SÍ en la del 17.          │
    │   • `criteria=AGREEMENT` es un valor aceptado (el 400 lista              │
    │     [SETTLEMENT, AGREEMENT]) pero devuelve 0 items siempre.              │
    │   • No hay otra fuente: /broker/operations (solapa Órdenes) está vacío   │
    │     —Meridiano opera por mesa—, /api/tickets no expone listado (404) y   │
    │     el calendario de la UI ni siquiera deja elegir fechas futuras.       │
    │   • El motivo de fondo: la lista es un LEDGER DE MOVIMIENTOS LIQUIDADOS  │
    │     (603 de 609 registros en `LIQUIDATED`, cero con liquidación futura). │
    │     Una operación no existe en esta API hasta que liquida.               │
    │                                                                          │
    │ Mientras tanto se reconcilia: se pide una ventana de liquidación hacia   │
    │ adelante y se archiva cada boleto bajo su `agreement_at`. Para backfills │
    │ es exacto (todo lo pasado ya liquidó). Para el día a día, un boleto que  │
    │ liquida más tarde aparece recién entonces, y lo levanta el `--delta` del │
    │ orquestador, que reprocesa los últimos días hábiles.                     │
    │                                                                          │
    │ ESTO NO ES LA SOLUCIÓN DEFINITIVA. Hay que conseguir de Allaria un       │
    │ endpoint que liste por concertación (o que `criteria=AGREEMENT`          │
    │ funcione). Cuando exista, `download_tickets` se simplifica a una         │
    │ consulta de un día y se puede borrar toda la lógica de ventana.          │
    └──────────────────────────────────────────────────────────────────────────┘

    Opciones de config relevantes:
        cuenta (str)          Nº de comitente. Default "131864" (MERIDIANO NORTE SA).
        totp_secret (str)     Secret TOTP para el 2FA de Auth0.
        ventana_dias (int)    Días hacia adelante de la ventana de liquidación.
                              Default 45. Ver la limitación de arriba.
        tipo_operacion (list) Tipos a descargar ("Venta FCE-eCheq", "Cauciones
                              Colocadoras", ...).
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self._persistent_context = None
        self._bearer: str = ""
        self._cuenta = str(self.opciones.get("cuenta", "131864"))
        self._ventana_dias = int(self.opciones.get("ventana_dias", _VENTANA_DIAS_DEFAULT))
        totp_raw = self.opciones.get("totp_secret", "")
        self._totp = pyotp.TOTP(self._resolve(totp_raw)) if totp_raw else None

    # ── Lifecycle: persistent context (preserva el device trust del 2FA) ──────

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

    # ── Login via Auth0 ───────────────────────────────────────────────────────

    async def login(self) -> bool:
        page    = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        # El bearer se captura de las requests del SPA. Importa filtrar por el HOST
        # EXACTO: login.api.allaria.cloud y market-data.api.allaria.cloud contienen
        # "api.allaria.cloud" como substring pero usan tokens de otra audiencia, y
        # quedarse con uno de esos hace fallar todo con 401.
        # Se guarda SIEMPRE el más reciente, no el primero: antes de autenticarse el
        # SPA ya hace llamadas con un token anónimo, y quedarse con ése da 401 en
        # todo lo posterior.
        def _capture(request):
            if not request.url.startswith(f"{_API}/"):
                return
            auth = request.headers.get("authorization")
            if auth:
                self._bearer = auth

        page.on("request", _capture)

        entrada = self.url_login or _URL_REDIRECT
        logger.info("[%s] Navegando a %s", self.nombre, entrada)
        await page.goto(entrada, wait_until="load", timeout=60_000)

        # El entry point redirige solo: o a la app (sesión viva en el perfil
        # persistente) o al formulario de Auth0. Hay que esperar a que resuelva:
        # decidir sobre la URL del propio redirect es lo que rompió el 2026-08-20.
        try:
            await page.wait_for_url(lambda u: _en_app(u) or _en_login(u), timeout=timeout)
        except Exception:
            pass
        logger.info("[%s] URL tras redirect inicial: %s", self.nombre, page.url)

        if _en_login(page.url):
            logger.info("[%s] Formulario Auth0 detectado — completando credenciales", self.nombre)
            await page.fill("input[type='email'], input[name='username']", self.usuario)
            await page.fill("input[type='password']", self.contrasena)
            await page.locator("button[type='submit']:not(:has-text('Google'))").first.click()
            await page.wait_for_timeout(4000)

            texto = await page.evaluate("document.body.innerText.slice(0, 500)")
            if any(k in texto.lower() for k in ("código", "verificación", "otp", "autenticador", "authenticator")):
                if not self._totp:
                    raise RuntimeError(f"[{self.nombre}] 2FA requerido pero no hay totp_secret configurado.")
                await self._completar_totp(page, timeout)

        try:
            await page.wait_for_url(_en_app, timeout=timeout)
        except Exception:
            raise RuntimeError(f"[{self.nombre}] Login Auth0 no completó — URL final: {page.url}")

        logger.info("[%s] Auth0 completado — URL: %s", self.nombre, page.url)

        # Descartar lo capturado antes de autenticarse: sólo vale un token emitido
        # después del login.
        self._bearer = ""

        # La pantalla de Actividad es la que dispara las llamadas a la API; sin
        # visitarla no hay token que capturar.
        await self._ir_a_actividad(timeout)

        if not self._bearer:
            raise RuntimeError(f"[{self.nombre}] No se pudo capturar el bearer token de {_API}")
        logger.info("[%s] Bearer token capturado", self.nombre)

        logger.info("[%s] Login exitoso — comitente %s", self.nombre, self._cuenta)
        return True

    async def _ir_a_actividad(self, timeout: int) -> None:
        """Navega a Actividad y espera a que aparezca el bearer, reintentando."""
        page = self._page
        url  = f"{_URL_APP}/actividad?accountId={self._cuenta}&period=HISTORY"
        for intento in range(1, 4):
            try:
                await page.goto(url, wait_until="networkidle", timeout=60_000)
            except Exception:
                pass
            t0 = time.time()
            while time.time() - t0 < 15 and not self._bearer:
                await page.wait_for_timeout(500)
            if self._bearer:
                return
            logger.info("[%s] Bearer aún no capturado (intento %d/3) — recargando", self.nombre, intento)

    async def _completar_totp(self, page, timeout: int) -> None:
        """Genera el código TOTP y lo completa en el formulario de Auth0."""
        for intento in range(1, 4):
            codigo = self._totp.now()
            logger.info("[%s] TOTP requerido — intento %d, código: %s", self.nombre, intento, codigo)
            campo = page.locator("input[type='text'], input[type='tel'], input[name='code']").first
            await campo.fill("")
            await campo.fill(codigo)
            await page.locator("button[type='submit']").first.click()
            await page.wait_for_timeout(4000)
            if not _en_login(page.url):
                logger.info("[%s] TOTP aceptado", self.nombre)
                return
            # Un código puede quedar a caballo de la ventana de 30s: esperar la siguiente
            await page.wait_for_timeout(31_000)
        raise RuntimeError(f"[{self.nombre}] No se pudo completar el 2FA tras 3 intentos")

    # ── API ───────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        # x-client-origin es obligatorio: sin ese header la API responde 401.
        return {
            "Authorization":   self._bearer,
            "accept":          "application/json, text/plain, */*",
            "accept-language": "es-AR",
            "x-client-origin": "ALLARIA",
            "referer":         f"{_URL_APP}/",
        }

    async def _listar_movimientos(self, desde: str, hasta: str) -> list[dict]:
        url = (f"{_API}/by-mas/movements?account-id={self._cuenta}"
               f"&from-date={desde}&to-date={hasta}"
               f"&company=ALLARIA&criteria=SETTLEMENT&currency=ARS")
        r = await self._persistent_context.request.get(url, headers=self._headers(), timeout=120_000)
        if r.status != 200:
            raise RuntimeError(f"[{self.nombre}] movements → {r.status}: {(await r.text())[:200]}")
        datos = json.loads(await r.text())
        return datos if isinstance(datos, list) else []

    async def _descargar_pdf(self, mov: dict) -> bytes | None:
        """
        Baja el boleto. Devuelve None si el movimiento no tiene comprobante
        (los movimientos de cuenta corriente devuelven 500).
        """
        url = (f"{_API}/api/tickets/{mov['operation_id']}"
               f"/accounts/{self._cuenta}/movements/{mov['id']}")
        r = await self._persistent_context.request.get(url, headers=self._headers(), timeout=120_000)
        if r.status != 200:
            logger.debug("[%s] %s sin comprobante (status %s)", self.nombre, mov["operation_id"], r.status)
            return None
        return await r.body()

    @staticmethod
    def _nro_de_boleto(pdf: bytes) -> str | None:
        """Extrae el número impreso en el PDF ('BOLETO #676265')."""
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf)) as doc:
                texto = doc.pages[0].extract_text() or ""
        except Exception:
            return None
        m = _RE_BOLETO.search(texto)
        return m.group(1) if m else None

    # ── Descarga ──────────────────────────────────────────────────────────────

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga los boletos concertados en `fecha` (YYYY-MM-DD).

        Ver la LIMITACIÓN CONOCIDA en el docstring de la clase: la API filtra por
        liquidación, así que se pide una ventana hacia adelante y se filtra
        localmente por `agreement_at`.
        """
        tipos_config = self.opciones.get("tipo_operacion", [])
        hasta = (date.fromisoformat(fecha) + timedelta(days=self._ventana_dias)).isoformat()

        movimientos = await self._listar_movimientos(fecha, hasta)
        logger.info("[%s] %d movimientos con liquidación entre %s y %s",
                    self.nombre, len(movimientos), fecha, hasta)

        # Sólo lo concertado en la fecha pedida
        del_dia = [
            m for m in movimientos
            if m.get("agreement_at") == fecha
            and (m.get("state") or "").upper() not in _ESTADOS_EXCLUIDOS
        ]
        if not del_dia:
            logger.info("[%s] Sin operaciones concertadas el %s", self.nombre, fecha)
            return []

        descargados: list[Path] = []
        for mov in del_dia:
            operacion = mov.get("market_operation_type")
            tipo = _TIPOS_POR_OPERACION.get(operacion)
            if not tipo:
                logger.debug("[%s] %s: '%s' no es un tipo que archivemos",
                             self.nombre, mov.get("operation_id"), operacion)
                continue
            if tipos_config and tipo not in tipos_config:
                logger.debug("[%s] %s: tipo '%s' no está en tipo_operacion",
                             self.nombre, mov.get("operation_id"), tipo)
                continue

            pdf = await self._descargar_pdf(mov)
            if not pdf:
                continue

            nro = self._nro_de_boleto(pdf)
            if not nro:
                # Sin número no se puede nombrar como el resto; se usa el id interno
                # para no perder el boleto, pero se avisa: el nombre en Drive va a
                # diferir de la convención y hay que revisarlo.
                nro = (mov.get("metadata") or {}).get("id") or mov["operation_id"].split("-")[0]
                logger.warning("[%s] No pude leer el nº de boleto del PDF de %s — uso %s",
                               self.nombre, mov.get("operation_id"), nro)

            tipo_dir = dest_dir / tipo
            tipo_dir.mkdir(parents=True, exist_ok=True)
            destino = tipo_dir / f"Boleto - {self.nombre} - {nro}.pdf"
            destino.write_bytes(pdf)
            descargados.append(destino)
            logger.info("[%s] %s | %s | boleto %s (liq. %s)",
                        self.nombre, tipo, operacion, nro, mov.get("settlement_at"))

        logger.info("[%s] Total descargados para %s: %d", self.nombre, fecha, len(descargados))
        return descargados

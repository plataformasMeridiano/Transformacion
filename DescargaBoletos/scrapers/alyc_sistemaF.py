import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT         = 30_000
_API_BASE        = "https://be.bancocmf.com.ar/api/v1/execute"
_URL_METROCORP   = "https://be.bancocmf.com.ar/metrocorp"
_URL_DESKTOP     = "https://be.bancocmf.com.ar/desktop"

# Palabras clave en descripcionOperacion que clasifican como Cauciones
_DEFAULT_CAUCION_KEYWORDS = frozenset({"CAUC", "COLOCACION"})

# Palabras clave que excluyen un movimiento aunque matchee caucion_keywords.
# "GARANTIA CAUCION TITULOS" → depósito de garantía en títulos, no una caución operativa.
# "APER. CAUC"               → movimiento interno del banco (extracto de cuenta),
#                              no un boleto de caución del mercado.
_DEFAULT_CAUCION_EXCLUDE = frozenset({"GARANTIA CAUCION", "APER. CAUC"})


class MetroCorpScraper(BaseScraper):
    """
    Scraper para Metrocorp (Banco CMF — be.bancocmf.com.ar).
    React SPA con autenticación OAuth via bearer token.

    Flujo de login (2 pasos):
        Step 1:
            1. Navegar a / (wait_until="networkidle")
            2. #document\\.number  → DNI
            3. #login\\.step1\\.username → usuario
            4. Click en button[type='submit']:has-text('Continuar')
            5. Esperar #login\\.step2\\.password
        Step 2:
            6. #login\\.step2\\.password → contraseña
            7. Click en button[type='submit']:has-text('Ingresar')
            8. Esperar URL contiene "desktop"
        Bearer token capturado de POST /oauth/token durante login.

    Flujo de descarga (download_tickets):
        Por cada cuenta configurada en opciones.cuentas:
        1. Abrir el environments-dropdown del header
        2. Hacer click en el botón con cuenta.display_name
        3. Esperar redirect a /desktop
        4. Navegar a /metrocorp (activa el contexto de sesión)
        5. POST metrocorp.list con cuenta.cuenta y cuenta.id_environment
           → lista de movimientos con descripcionOperacion, numeroBoleto, etc.
        6. Clasificar por tipo (Cauciones / Pases)
        7. Para cada movimiento de cada tipo configurado:
           POST metrocorp.downloadList (format=pdf, summary con ese único movimiento)
           → PDF bytes
        8. Guardar en dest_dir/{cuenta.nombre}/{tipo}/{numeroBoleto}.pdf

    Configuración relevante en opciones:
        cuentas (list[dict])  Lista de cuentas:
                              {"nombre": str, "display_name": str,
                               "cuenta": str, "id_environment": int}
                              Si está vacío o ausente: usa opciones.cuenta y
                              opciones.id_environment (compatibilidad hacia atrás).
        caucion_keywords (list[str]) Palabras en descripcionOperacion → Cauciones.
                                      Default: ["CAUC", "COLOCACION"].
        tipo_operacion   (list[str]) Tipos a descargar: "Cauciones", "Pases".
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        # Metrocorp requiere headless=False (detección de bot sin display)
        self.headless = False

        # Valores de fallback para compatibilidad con configs sin "cuentas"
        self._cuenta_default   = self.opciones.get("cuenta", "33460")
        self._id_env_default   = self.opciones.get("id_environment", 407)

        keywords = self.opciones.get("caucion_keywords")
        self._caucion_kw = (
            frozenset(k.upper() for k in keywords)
            if keywords is not None
            else _DEFAULT_CAUCION_KEYWORDS
        )
        excludes = self.opciones.get("caucion_exclude_keywords")
        self._caucion_exclude = (
            frozenset(k.upper() for k in excludes)
            if excludes is not None
            else _DEFAULT_CAUCION_EXCLUDE
        )
        self._bearer: str = ""
        self._dni = self._resolve(alyc_config.get("documento", ""))

    # ── Utilidades ──────────────────────────────────────────────────────────

    def _classify(self, descripcion: str) -> str | None:
        """
        Clasifica un movimiento por su descripcionOperacion.
        Retorna "Cauciones", "Pases", o None (ignorar).

        Lógica:
          1. Si la descripción coincide con alguna caucion_exclude_keyword → None
          2. Si coincide con alguna caucion_keyword → "Cauciones"
          3. De lo contrario → "Pases"

        Motivo del exclude:
          "GARANTIA CAUCION TITULOS" contiene "CAUCION" pero es depósito de garantía,
          no una operación de caución. Se excluye explícitamente.

        Motivo de "CAUCION" en lugar de "CAUC":
          Las operaciones internas del banco usan "APER. CAUC TOMADORA" (abreviado),
          que genera PDFs de "Movimientos" (extracto), no boletos reales.
          Los boletos de exchange usan siempre "CAUCION" completo.
        """
        desc_up = descripcion.upper()
        logger.debug("[%s] _classify: '%s'", self.nombre, descripcion)
        if any(kw in desc_up for kw in self._caucion_exclude):
            logger.debug("[%s] Excluido por exclude_keyword: '%s'", self.nombre, descripcion)
            return None
        if any(kw in desc_up for kw in self._caucion_kw):
            return "Cauciones"
        return "Pases"

    @staticmethod
    def _make_iso(fecha_str: str) -> str:
        """Convierte dd/mm/yyyy a ISO8601 UTC (medianoche Argentina = 03:00 UTC)."""
        dt = datetime.strptime(fecha_str, "%d/%m/%Y")
        return dt.replace(hour=3, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    async def _api_post(self, endpoint: str, body: dict) -> dict:
        """
        Realiza POST a /api/v1/execute/{endpoint} via fetch() del browser.
        Usa el bearer token capturado durante el login.
        """
        page = self._page
        result = await page.evaluate(
            """async ([url, bodyStr, auth]) => {
                try {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json;charset=UTF-8',
                            'Accept': 'application/json, application/octet-stream',
                            'Authorization': auth
                        },
                        credentials: 'include',
                        body: bodyStr
                    });
                    const bodyAb = await r.arrayBuffer();
                    const bytes = new Uint8Array(bodyAb);
                    let b64 = '';
                    const CHUNK = 8192;
                    for (let i = 0; i < bytes.byteLength; i += CHUNK)
                        b64 += String.fromCharCode(
                            ...bytes.subarray(i, Math.min(i + CHUNK, bytes.byteLength))
                        );
                    return {
                        ok: r.ok, status: r.status,
                        ct: r.headers.get('content-type') || '',
                        b64: btoa(b64), len: bytes.byteLength
                    };
                } catch(e) {
                    return { ok: false, error: e.toString() };
                }
            }""",
            [f"{_API_BASE}/{endpoint}", json.dumps(body), self._bearer],
        )
        if not result.get("ok") and result.get("status", 0) not in (200, 201):
            raise RuntimeError(
                f"[{self.nombre}] {endpoint} → status={result.get('status')} "
                f"error={result.get('error')}"
            )
        raw = base64.b64decode(result["b64"])
        if b"%PDF" in raw[:10]:
            return {"_pdf_binary": raw}
        if "json" in result.get("ct", ""):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        return {"_raw": raw[:200].decode("utf-8", errors="replace")}

    # ── Login ────────────────────────────────────────────────────────────────

    async def login(self) -> bool:
        page    = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        # Capturar bearer token del flujo OAuth
        async def _capture_token(resp):
            if "/oauth/token" in resp.url:
                try:
                    body = await resp.body()
                    j    = json.loads(body)
                    if "access_token" in j:
                        self._bearer = f"bearer {j['access_token']}"
                        logger.info("[%s] Bearer token capturado", self.nombre)
                except Exception:
                    pass

        page.on("response", _capture_token)

        logger.info("[%s] Navegando a %s", self.nombre, self.url_login)
        await page.goto(self.url_login, wait_until="load", timeout=60_000)

        # Esperar hasta que el SPA muestre el form de login O redirija a /desktop
        # (el redirect puede ser vía pushState, sin network traffic adicional)
        await page.wait_for_function(
            "() => window.location.pathname.includes('desktop')"
            " || !!document.querySelector('#document\\\\.number')",
            timeout=45_000,
        )

        # Si hay sesión activa el sitio redirecciona a /desktop
        if "desktop" in page.url:
            logger.info("[%s] Sesión activa detectada — reutilizando sin re-login", self.nombre)
            return True

        # Step 1: DNI + usuario
        logger.info("[%s] Step 1: DNI + usuario", self.nombre)
        await page.wait_for_selector("#document\\.number", timeout=timeout)
        await page.fill("#document\\.number", self._dni)
        await page.fill("#login\\.step1\\.username", self.usuario)
        await page.click("button[type='submit']:has-text('Continuar')")

        # Step 2: contraseña (el portal puede tardar > 30s en renderizar el campo)
        logger.info("[%s] Step 2: contraseña", self.nombre)
        await page.wait_for_selector("#login\\.step2\\.password", timeout=max(timeout, 60_000))
        await page.fill("#login\\.step2\\.password", self.contrasena)
        await page.click("button[type='submit']:has-text('Ingresar')")

        await page.wait_for_url(lambda u: "desktop" in u, timeout=timeout)
        await page.wait_for_timeout(2000)

        logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
        return True

    # ── Cambio de ambiente ────────────────────────────────────────────────────

    async def _switch_environment(self, display_name: str, timeout: int) -> None:
        """
        Cambia el ambiente activo (empresa) via el environments-dropdown del header.
        Abre el dropdown, hace click en el botón con el texto `display_name`,
        y espera el redirect a /desktop.
        """
        page = self._page
        # Asegurarse de estar en una página con el header (ej: /desktop o /metrocorp)
        if "desktop" not in page.url and "metrocorp" not in page.url:
            await page.goto(_URL_DESKTOP, wait_until="networkidle", timeout=timeout)
            await page.wait_for_timeout(1000)

        logger.info("[%s] Abriendo environments-dropdown", self.nombre)
        env_btn = page.locator("li.environments-dropdown button").first

        # Verificar rápidamente si el botón está disponible
        try:
            await env_btn.wait_for(state="visible", timeout=8_000)
        except Exception:
            # Sesión degradada: navegar a /desktop para refrescar el header
            logger.warning(
                "[%s] Dropdown no disponible — navegando a /desktop para refrescar", self.nombre
            )
            await page.goto(_URL_DESKTOP, wait_until="networkidle", timeout=timeout)
            await page.wait_for_timeout(2000)
            env_btn = page.locator("li.environments-dropdown button").first
            try:
                await env_btn.wait_for(state="visible", timeout=10_000)
            except Exception:
                # Último recurso: re-login completo
                logger.warning("[%s] Dropdown aún no disponible — re-login completo", self.nombre)
                await self.login()
                env_btn = page.locator("li.environments-dropdown button").first

        await env_btn.click(timeout=timeout)
        await page.wait_for_timeout(800)

        logger.info("[%s] Seleccionando ambiente: %s", self.nombre, display_name)
        target = page.get_by_text(display_name, exact=True).first
        # force=True necesario: el div.drawer-content-inner intercepta pointer events
        await target.click(force=True, timeout=timeout)

        await page.wait_for_url(lambda u: "desktop" in u, timeout=timeout)
        await page.wait_for_timeout(2000)

    # ── Descarga ─────────────────────────────────────────────────────────────

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
        tipos_config = self.opciones.get("tipo_operacion", [])

        fecha_dt  = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_fmt = fecha_dt.strftime("%d/%m/%Y")
        iso_date  = self._make_iso(fecha_fmt)

        # Construir lista de cuentas a procesar
        cuentas: list[dict] = self.opciones.get("cuentas", [])
        if not cuentas:
            # Compatibilidad hacia atrás: usar opciones.cuenta / opciones.id_environment
            cuentas = [{"nombre": "", "display_name": "",
                        "cuenta": self._cuenta_default,
                        "id_environment": self._id_env_default}]

        multi = len(cuentas) > 1 and any(c.get("nombre") for c in cuentas)

        if not self._bearer:
            logger.error("[%s] Bearer token no capturado durante el login", self.nombre)
            return []

        downloaded: list[Path] = []

        for i, cuenta in enumerate(cuentas):
            display_name = cuenta.get("display_name", "")
            cuenta_id    = cuenta.get("cuenta", self._cuenta_default)
            id_env       = cuenta.get("id_environment", self._id_env_default)
            cta_nombre   = cuenta.get("nombre", "")

            # ── 1. Cambiar ambiente si corresponde ────────────────────────────
            if display_name:
                logger.info("[%s] Cambiando a ambiente '%s'",
                            self.nombre, display_name)
                await self._switch_environment(display_name, timeout)

            # ── 2. Activar el contexto de sesión en /metrocorp ────────────────
            logger.info("[%s] Navegando a /metrocorp [%s]",
                        self.nombre, cta_nombre or "default")
            await page.goto(_URL_METROCORP, wait_until="networkidle", timeout=timeout)
            await page.wait_for_timeout(2000)

            # ── 3. Obtener movimientos del día ────────────────────────────────
            logger.info("[%s] Consultando movimientos para %s [%s]",
                        self.nombre, fecha, cta_nombre or "default")
            list_resp = await self._api_post("metrocorp.list", {
                "optionSelected": "movements",
                "principalAccount": cuenta_id,
                "species": "all",
                "date":     iso_date,
                "dateFrom": iso_date,
                "dateTo":   iso_date,
                "page": 1,
                "idEnvironment": id_env,
                "lang": "es",
                "channel": "frontend",
            })

            if "_raw" in list_resp or "_pdf_binary" in list_resp:
                logger.error("[%s] Respuesta inesperada de metrocorp.list [%s]: %s",
                             self.nombre, cta_nombre or "default", str(list_resp)[:200])
                continue

            data      = list_resp.get("data", {})
            code      = list_resp.get("code", "")
            movements = data.get("movements", [])

            if code != "COR000I":
                logger.warning("[%s] metrocorp.list code=%s [%s] para %s",
                               self.nombre, code, cta_nombre or "default", fecha)
                continue

            logger.info("[%s] Movimientos el %s [%s]: %d",
                        self.nombre, fecha, cta_nombre or "default", len(movements))

            if not movements:
                logger.info("[%s] Sin movimientos para %s [%s]",
                            self.nombre, fecha, cta_nombre or "default")
                continue

            # ── 4. Clasificar movimientos ─────────────────────────────────────
            by_tipo: dict[str, list] = {"Cauciones": [], "Pases": []}
            for m in movements:
                desc = m.get("descripcionOperacion", "")
                tipo = self._classify(desc)
                if tipo is not None:
                    by_tipo[tipo].append(m)

            dest_base = dest_dir / cta_nombre if (multi and cta_nombre) else dest_dir

            # ── 5. Descargar un PDF por boleto ────────────────────────────────
            for tipo in tipos_config:
                movs_tipo = by_tipo.get(tipo, [])
                if not movs_tipo:
                    logger.info("[%s] Sin movimientos de tipo '%s' [%s] para %s",
                                self.nombre, tipo, cta_nombre or "default", fecha)
                    continue

                dest_tipo_dir = dest_base / tipo
                dest_tipo_dir.mkdir(parents=True, exist_ok=True)

                for idx, m in enumerate(movs_tipo):
                    nro_lista = m.get("numeroBoleto", "").lstrip("0") or m.get("numeroBoleto", "")
                    ref       = m.get("referenciaMinuta", "")

                    if not ref:
                        logger.warning("[%s] Sin referenciaMinuta — omitiendo idx=%d [%s]",
                                       self.nombre, idx, cta_nombre or "default")
                        continue

                    logger.info("[%s] Descargando boleto ref=%s nro_lista=%s (%s) [%s]",
                                self.nombre, ref, nro_lista, tipo, cta_nombre or "default")

                    try:
                        # ── Paso 1: obtener detalle del movimiento ────────────
                        det_resp = await self._api_post("metrocorp.detail", {
                            "reference":     ref,
                            "idEnvironment": id_env,
                            "lang":          "es",
                            "channel":       "frontend",
                        })
                        if det_resp.get("code") != "COR000I":
                            logger.warning("[%s] detail fallido — ref=%s code=%s [%s]",
                                           self.nombre, ref, det_resp.get("code"), cta_nombre or "default")
                            continue

                        mov_detail = det_resp["data"]["movementDetail"]

                        # Número de boleto: desde detail (con ceros stripped) o fallback a lista
                        nro = (mov_detail.get("numBoleto") or "").strip().lstrip("0")
                        if not nro:
                            nro = nro_lista or f"{idx + 1:03d}"

                        # Currency: "$" para pesos, "U$S" para dólares
                        cod_espe = m.get("codEspe", "").strip().upper()
                        currency = "$" if "PESO" in cod_espe or cod_espe in ("$", "ARS", "") else "U$S"

                        # ── Paso 2: descargar PDF del detalle ─────────────────
                        dl_resp = await self._api_post("metrocorp.downloadDetail", {
                            "detail": {
                                **mov_detail,
                                "codEspe":       m.get("codEspe", ""),
                                "Nombre":        m.get("Nombre", ""),
                                "Apellido":      m.get("Apellido", ""),
                                "currency":      currency,
                                "observaciones": m.get("observaciones", ""),
                            },
                            "format":        "pdf",
                            "idEnvironment": id_env,
                            "lang":          "es",
                            "channel":       "frontend",
                        })

                        dl_code     = dl_resp.get("code", "")
                        content_b64 = dl_resp.get("data", {}).get("content", "") if isinstance(dl_resp.get("data"), dict) else ""

                        # Extraer bytes del PDF
                        pdf_bytes = None
                        if "_pdf_binary" in dl_resp:
                            pdf_bytes = dl_resp["_pdf_binary"]
                        elif dl_code == "COR000I" and content_b64:
                            pdf_bytes = base64.b64decode(content_b64)
                        else:
                            logger.warning(
                                "[%s] downloadDetail fallido — ref=%s nro=%s code=%s [%s]",
                                self.nombre, ref, nro, dl_code, cta_nombre or "default",
                            )
                            continue

                        if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
                            logger.warning(
                                "[%s] Respuesta no es PDF — ref=%s nro=%s [%s]",
                                self.nombre, ref, nro, cta_nombre or "default",
                            )
                            continue

                        fname = dest_tipo_dir / f"{nro}.pdf"
                        fname.write_bytes(pdf_bytes)
                        logger.info("[%s] Guardado: %s (%d bytes)",
                                    self.nombre, fname.name, len(pdf_bytes))
                        downloaded.append(fname)

                    except Exception as exc:
                        logger.error(
                            "[%s] Error descargando boleto ref=%s [%s] — %s: %s",
                            self.nombre, ref, cta_nombre or "default",
                            type(exc).__name__, exc,
                        )

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

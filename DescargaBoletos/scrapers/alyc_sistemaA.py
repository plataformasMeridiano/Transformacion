import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000
_PROFILE_DIR = Path("browser_profiles/puente")

_BASE_URL        = "https://www.puentenet.com"
_URL_MOVIMIENTOS = f"{_BASE_URL}/cuentas/mi-cuenta/movimientos"

# Mapeo nombre config → valores del select #descripcionFiltro que le corresponden
_TIPO_A_FILTROS: dict[str, list[str]] = {
    "Cauciones": ["Caución Tomadora", "Caución Colocadora", "Cierre Caución"],
    "Pases":     ["Pase Tomador",     "Pase Colocador"],
}

# Regex para extraer idMovimiento de href="/...?idCuenta=X&idMovimiento=Y"
_RE_ID_MOV = re.compile(r"idMovimiento=(\d+)")
_RE_CD_NRO = re.compile(r"Movimiento\s+(\d+)", re.IGNORECASE)


class PuenteScraper(BaseScraper):
    """
    Scraper para el portal Puente (sistemaA).
    Soporta múltiples ALYCs que comparten el mismo portal con distintas credenciales.

    Flujo de login:
        1. Tipo de documento  (select, valor configurable — por defecto "DNI")
        2. Nro. Documento     (input por placeholder, sin id)
        3. Usuario            (#input_username)
        4. Contraseña         (#input_password)
        5. Click en botón "Ingresar" dentro del #loginForm
        6. Espera a que la URL cambie (confirma login exitoso)
        7. Detección de pinForm — si aparece, lanza excepción (no implementado)

    Flujo de descarga (download_tickets):
        Por cada tipo configurado en opciones.tipo_operacion:
        Por cada valor de filtro que corresponde a ese tipo (_TIPO_A_FILTROS):
        Por cada cuenta en el select #idCuenta:
        1. Setear #idCuenta, #fechaDesde, #fechaHasta, #descripcionFiltro
        2. Click en #traerMovimientos y esperar networkidle
        3. Extraer links a descargar-pdf-movimiento
        4. GET al link → guardar PDF en dest_dir/{tipo}/{idMovimiento}.pdf
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self.documento = self._resolve(alyc_config["documento"])
        self.tipo_documento = alyc_config.get("tipo_documento", "DNI")
        self._persistent_context = None

    # ── Lifecycle: persistent context + real Chrome ───────────────────────────

    async def __aenter__(self):
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._persistent_context = await self._playwright.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=self.headless,
            executable_path="/usr/bin/google-chrome-stable",
            slow_mo=50,
            accept_downloads=True,
            viewport={"width": 1366, "height": 768},
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

    async def login(self) -> bool:
        page = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)
        url_login = self.url_login

        logger.info("[%s] Navegando a %s", self.nombre, url_login)
        await page.goto(url_login, wait_until="load", timeout=timeout)

        # Seleccionar tipo de documento solo si difiere del default (DNI)
        if self.tipo_documento != "DNI":
            logger.info("[%s] Seleccionando tipo de documento: %s", self.nombre, self.tipo_documento)
            await page.locator("#loginForm select").select_option(label=self.tipo_documento)

        logger.info("[%s] Completando formulario de login", self.nombre)
        await page.locator("#loginForm input[placeholder='Nro. documento']").fill(self.documento)
        await page.locator("#loginForm #input_username").fill(self.usuario)
        await page.locator("#loginForm #input_password").fill(self.contrasena)

        logger.info("[%s] Enviando credenciales", self.nombre)
        await page.locator("#loginForm").get_by_text("Ingresar", exact=True).click()

        await page.wait_for_url(lambda url: "/login" not in url, timeout=timeout)

        if await page.locator("#pinForm").is_visible():
            logger.warning("[%s] Se detectó el formulario de PIN", self.nombre)
            raise NotImplementedError(
                f"[{self.nombre}] El portal solicita un PIN después del login. "
                "Este flujo no está implementado todavía — revisar manualmente."
            )

        logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
        return True

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga los comprobantes PDF de la fecha indicada (YYYY-MM-DD).

        Estructura de destino:
            dest_dir/
            ├── Cauciones/
            │   └── {idMovimiento}.pdf
            └── Pases/
                └── {idMovimiento}.pdf

        Itera sobre todos los tipos de operación configurados y sobre todas
        las cuentas disponibles en el select #idCuenta.
        """
        page         = self._page
        timeout      = self.opciones.get("timeout_ms", _TIMEOUT)
        tipos_config = self.opciones.get("tipo_operacion", [])
        fecha_fmt    = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")

        logger.info("[%s] Navegando a Movimientos", self.nombre)
        await page.goto(_URL_MOVIMIENTOS, wait_until="load", timeout=timeout)

        # Leer todas las cuentas disponibles en el select
        cuentas = await page.evaluate("""
            () => [...document.querySelectorAll('#idCuenta option')]
                    .map(o => ({ value: o.value, label: o.text.trim() }))
                    .filter(o => o.value)
        """)
        logger.info("[%s] Cuentas: %d", self.nombre, len(cuentas))

        downloaded: list[Path] = []

        for tipo in tipos_config:
            filtros = _TIPO_A_FILTROS.get(tipo)
            if not filtros:
                logger.warning("[%s] Tipo '%s' sin filtros definidos — omitiendo", self.nombre, tipo)
                continue

            dest_tipo_dir = dest_dir / tipo
            dest_tipo_dir.mkdir(parents=True, exist_ok=True)

            for filtro_val in filtros:
                for cuenta in cuentas:
                    logger.info("[%s] %s / %s / cuenta %s",
                                self.nombre, tipo, filtro_val, cuenta["label"])

                    # ── Setear filtros ──────────────────────────────────────
                    # Seleccionar cuenta y descripción PRIMERO.
                    # Cambiar el select puede disparar un AJAX automático.
                    # Seteamos las fechas AL FINAL (después de los selects)
                    # para evitar que AJAX triggered por los selects las pise.
                    await page.select_option("#idCuenta", value=cuenta["value"])
                    await asyncio.sleep(2)

                    await page.select_option("#descripcionFiltro", value=filtro_val)
                    await asyncio.sleep(2)

                    # Los inputs de fecha están controlados por AngularJS.
                    # En modo headless/xvfb el keyboard.type no actualiza el ng-model.
                    # Usamos dispatchEvent JS (input + change + blur) que funciona en
                    # cualquier modo de display.
                    await page.evaluate(f"""
                        () => {{
                            ['#fechaDesde', '#fechaHasta'].forEach(sel => {{
                                const el = document.querySelector(sel);
                                if (!el) return;
                                el.value = '{fecha_fmt}';
                                ['input', 'change', 'blur'].forEach(ev =>
                                    el.dispatchEvent(new Event(ev, {{bubbles: true}}))
                                );
                            }});
                        }}
                    """)
                    await asyncio.sleep(0.5)

                    # Verificar que los valores quedaron en el DOM
                    val_desde = await page.input_value("#fechaDesde")
                    val_hasta = await page.input_value("#fechaHasta")
                    logger.info("[%s]   fechas seteadas: %s → %s", self.nombre, val_desde, val_hasta)

                    # Esperar la respuesta específica del endpoint de movimientos
                    async with page.expect_response(
                        lambda r: "obtener-resultado-movimientos" in r.url,
                        timeout=timeout,
                    ) as resp_info:
                        await page.click("#traerMovimientos")
                    resp = await resp_info.value
                    await asyncio.sleep(1)

                    # Intentar leer el JSON de respuesta para validar fechas
                    try:
                        resp_json = await resp.json()
                        total_server = len(resp_json) if isinstance(resp_json, list) else -1
                        logger.info("[%s]   Respuesta servidor: %d items", self.nombre, total_server)
                    except Exception:
                        resp_json = None

                    # ── Extraer links de descarga — solo los del resultado actual ─
                    # Navegar de nuevo a la página resetea el DOM entre búsquedas
                    # y evita que links de búsquedas anteriores se acumulen.
                    movimientos = await page.evaluate(f"""
                        () => {{
                            const fecha = '{fecha_fmt}';
                            return [...document.querySelectorAll(
                                    'a[href*="descargar-pdf-movimiento"]')]
                                    .map(a => {{
                                        const row = a.closest('tr');
                                        const cells = row
                                            ? [...row.querySelectorAll('td')]
                                                .map(td => td.innerText.trim())
                                            : [];
                                        return {{
                                            href: a.getAttribute('href'),
                                            cells: cells,
                                            rowText: row ? row.innerText : '',
                                        }};
                                    }});
                        }}
                    """)

                    # Filtro client-side ESTRICTO — tres condiciones:
                    # 1. Fecha de CONCERTACIÓN == fecha pedida (sin fallback liquidación:
                    #    el fallback incluía cauciones de día anterior en carpeta Pases).
                    # 2. El texto de la fila debe contener el tipo de filtro actual
                    #    (evita FCI/garantías que el portal mezcla con Pases).
                    # 3. Si no podemos verificar la fecha (pocas celdas) → SKIP.
                    filtro_lower = filtro_val.lower()
                    movimientos_filtrados = [
                        m for m in movimientos
                        if len(m["cells"]) >= 3
                        and m["cells"][2] == fecha_fmt
                        and filtro_lower in m["rowText"].lower()
                    ]
                    if len(movimientos) > 0 and len(movimientos_filtrados) == 0:
                        logger.warning(
                            "[%s]   AVISO: %d links en DOM pero NINGUNO con fecha %s y tipo '%s' — "
                            "posible fallo del filtro server-side (Angular model no actualizado). "
                            "Verificar val_desde/val_hasta arriba.",
                            self.nombre, len(movimientos), fecha_fmt, filtro_val,
                        )
                    logger.info("[%s]   Movimientos: %d (de %d en DOM, filtro fecha=%s tipo='%s')",
                                self.nombre, len(movimientos_filtrados),
                                len(movimientos), fecha_fmt, filtro_val)
                    movimientos = movimientos_filtrados

                    # ── Descargar PDFs ──────────────────────────────────────
                    for mov in movimientos:
                        m = _RE_ID_MOV.search(mov.get("href", ""))
                        if not m:
                            logger.warning("[%s] Sin idMovimiento en %s", self.nombre, mov["href"])
                            continue

                        id_mov    = m.group(1)

                        # Skip-check via provisional_path ({idMovimiento}.pdf).
                        # Puede ser:
                        #   a) Marker de texto (< 100 bytes): contiene el nro de boleto real.
                        #      Se crea después de cada descarga exitosa con CD.
                        #   b) PDF real (≥ 100 bytes): fallback cuando no hay Content-Disposition.
                        provisional_path = dest_tipo_dir / f"{id_mov}.pdf"
                        if provisional_path.exists():
                            if provisional_path.stat().st_size < 100:
                                # Marker → buscar archivo real
                                nro = provisional_path.read_text().strip()
                                real_path = dest_tipo_dir / f"{nro}.pdf"
                                if real_path.exists():
                                    logger.debug("[%s] Ya existe (marker → %s)", self.nombre, real_path.name)
                                    downloaded.append(real_path)
                                    continue
                                # Marker sin archivo real → re-descargar
                            else:
                                # PDF real en provisional_path (fallback sin CD)
                                logger.debug("[%s] Ya existe: %s", self.nombre, provisional_path.name)
                                downloaded.append(provisional_path)
                                continue

                        logger.info("[%s] Descargando %s/%s", self.nombre, tipo, id_mov)
                        try:
                            resp      = await page.context.request.get(
                                f"{_BASE_URL}{mov['href']}",
                                timeout=timeout,
                            )
                            pdf_bytes = await resp.body()

                            # Extraer número de boleto del Content-Disposition
                            # Ej: filename="13841 - Movimiento 20248.pdf"
                            cd = resp.headers.get("content-disposition", "")
                            nro_match = _RE_CD_NRO.search(cd)
                            if nro_match and nro_match.group(1) != "0":
                                nro_boleto = nro_match.group(1)
                                dest_path  = dest_tipo_dir / f"{nro_boleto}.pdf"
                                logger.info("[%s] Nro boleto desde Content-Disposition: %s", self.nombre, nro_boleto)
                            elif nro_match and nro_match.group(1) == "0":
                                logger.warning("[%s] Boleto con nro=0 en Content-Disposition ('%s') — operación no estándar, omitiendo", self.nombre, cd)
                                continue
                            else:
                                dest_path = provisional_path
                                logger.warning("[%s] Sin nro boleto en Content-Disposition ('%s') — usando idMovimiento", self.nombre, cd)

                            dest_path.write_bytes(pdf_bytes)
                            logger.info("[%s] Guardado: %s (%d bytes)",
                                        self.nombre, dest_path.name, len(pdf_bytes))
                            # Escribir marker en provisional_path para que re-runs hagan skip
                            # (solo cuando se guardó con nombre distinto al provisional)
                            if dest_path != provisional_path:
                                provisional_path.write_text(nro_boleto)
                            downloaded.append(dest_path)

                        except Exception as exc:
                            logger.error("[%s] Error en %s — %s: %s",
                                         self.nombre, id_mov, type(exc).__name__, exc)

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

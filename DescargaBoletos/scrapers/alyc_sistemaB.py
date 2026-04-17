import asyncio
import logging
from datetime import datetime
from pathlib import Path

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000

# Códigos de tipo de operación por portal (columna de la grilla)
# Pueden sobreescribirse con opciones["caucion_codes"] en config.json
_DEFAULT_CAUCION_CODES = frozenset({"APTOMCONC", "APTOMFUTC"})  # ADCAP


class AdcapScraper(BaseScraper):
    """
    Scraper para portales VBhome / Unisync (AngularJS + Material Design).
    Usado por ADCAP y BACS (Toronto Inversiones), entre otros.

    Flujo de login:
        1. Esperar a que Angular renderice el formulario en ng-view
        2. Usuario  (#input_0 / name=txtID)
        3. Clave    (#input_1 / name=txtPwd)
        4. Click en #btnIngresar (ng-click='logIn()')
        5. Esperar a que el hash cambie de #!/login (confirma login aceptado)

    Flujo de descarga (download_tickets):
        Por cada cuenta configurada en opciones.cuentas:
        1. Cambiar el md-select[ng-model='rootModel.selectedCounts'] clickeando
           la md-option cuyo label coincide con cuenta.label
        2. Esperar a que Angular refresque la vista (ng-change='reload(...)')
        3. Navegar a la sección BOLETOS via md-item-content[ng-click]
        4. Filtrar filas tr[data-id] por fecha de concertación (cells[2], dd/mm/yyyy)
        5. Para cada fila: click en a.icon-file-pdf → dialog → descargar PDF
        6. Guardar en dest_dir/{cuenta.nombre}/{tipo}/ si hay múltiples cuentas

    Configuración relevante en opciones:
        cuentas        (list[dict])  Lista de cuentas: {"nombre": str, "label": str}
                                     label debe coincidir con el texto del md-option.
                                     Si está vacío o ausente: usa la cuenta por defecto.
        caucion_codes  (list[str])   Códigos que identifican cauciones en la grilla.
                                     Default: ["APTOMCONC", "APTOMFUTC"] (ADCAP).
                                     Ejemplo BACS: ["VENTACNG", "COMPRACNG"].
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

    def _classify_tipo(self, cells: list[str]) -> str:
        """Clasifica un boleto como 'Cauciones' o 'Pases' según los valores de la fila."""
        for cell in cells:
            if cell.strip().upper() in self._caucion_codes:
                return "Cauciones"
        return "Pases"

    async def login(self) -> bool:
        page = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        logger.info("[%s] Navegando a %s", self.nombre, self.url_login)
        await page.goto(self.url_login, wait_until="domcontentloaded", timeout=timeout)

        # Esperar a que Angular inyecte el formulario en ng-view
        logger.info("[%s] Esperando renderizado del formulario", self.nombre)
        await page.wait_for_selector("#input_0", timeout=timeout)

        logger.info("[%s] Completando formulario de login", self.nombre)
        await page.fill("#input_0", self.usuario)
        await page.fill("#input_1", self.contrasena)

        logger.info("[%s] Enviando credenciales", self.nombre)
        await page.click("#btnIngresar")

        # Con hash routing, la URL base no cambia — esperamos que el hash deje de ser #!/login
        await page.wait_for_url(lambda url: "#!/login" not in url, timeout=timeout)

        logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
        return True

    async def _switch_cuenta(self, label: str, timeout: int) -> None:
        """
        Cambia la cuenta activa en el md-select de cuentas del portal VBhome.
        Abre el selector, hace click en la opción cuyo label coincide,
        y espera a que Angular ejecute ng-change='reload(...)'.
        """
        page = self._page
        md_select = page.locator("md-select[ng-model='rootModel.selectedCounts']")
        await md_select.click(timeout=timeout)
        await asyncio.sleep(1)
        # Las md-option pueden aparecer en un panel fuera del árbol del md-select
        option = page.locator(f"md-option[label='{label}']")
        await option.click(timeout=timeout)
        # ng-change dispara reload() — esperar que Angular actualice la vista
        await asyncio.sleep(3)

    async def _navegar_boletos(self, timeout: int) -> None:
        """
        Navega a la sección BOLETOS forzando un reload completo del controller Angular.

        Usa page.goto() con la URL directa (desktop.html#!/boletos) en lugar de hacer
        click en el menú lateral. El click al menú es un no-op cuando ya estamos en
        #!/boletos, y después de ~23 iteraciones el controller Angular se degrada y
        scope.filter() deja de hacer fetch al servidor. El goto() fuerza reinicialización.
        """
        page = self._page
        boletos_url = (
            self.url_login.split("#")[0]
            .replace("login.html", "desktop.html")
            + "#!/boletos"
        )
        await page.goto(boletos_url, wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(4)
        logger.info("[%s] Vista BOLETOS cargada — URL: %s", self.nombre, page.url)

    async def _aplicar_filtro_fecha(self, fecha_iso: str) -> None:
        """
        Aplica el filtro de fecha en la vista BOLETOS via scope Angular.
        Abre el diálogo de filtro, setea fechaDesde y fechaHasta al mismo día,
        y llama filter() — sin interactuar con el widget datepicker (evita spinner loop).

        fecha_iso: str en formato YYYY-MM-DD.
        """
        page = self._page
        # Abrir el diálogo de filtro BOLETOS
        await page.evaluate("() => { const ic = document.querySelector('span.icon-filter'); if (ic) ic.click(); }")
        await asyncio.sleep(1.5)

        result = await page.evaluate(f"""
            () => {{
                const dialog = document.querySelector('.md-dialog-container');
                if (!dialog) return 'no-dialog';
                const scope = angular.element(dialog).scope();
                if (!scope?.filters) return 'no-filters';
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

    async def _leer_filas(self, fecha_fmt: str):
        """Lee las filas de la grilla filtrando por fecha de concertación.

        Solo incluye filas que ya tienen el ícono PDF en el DOM.
        Llamar esto después de scrollear la tabla completa para forzar
        el lazy-rendering de todos los íconos.
        """
        page = self._page
        return await page.evaluate(f"""
            () => {{
                const fecha = '{fecha_fmt}';
                const result = [];
                for (const row of document.querySelectorAll('table tr')) {{
                    if (row.querySelectorAll('td').length < 6) continue;
                    if (!row.getAttribute('data-id')) continue;
                    if (!row.querySelector('a.icon-file-pdf.app_gridIcon')) continue;
                    const cells = [...row.querySelectorAll('td')].map(td => td.innerText.trim());
                    if (cells[2] !== fecha) continue;
                    result.push({{ dataId: row.getAttribute('data-id'), cells }});
                }}
                return result;
            }}
        """)

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
        fecha_fmt = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")

        multi = len(cuentas) > 1 and any(c.get("nombre") for c in cuentas)

        # ── 1. Esperar que el dashboard cargue (post-login) ───────────────────
        logger.info("[%s] Esperando que el dashboard cargue", self.nombre)
        await asyncio.sleep(5)

        downloaded: list[Path] = []

        for i, cuenta in enumerate(cuentas):
            label      = cuenta.get("label", "")
            cta_nombre = cuenta.get("nombre", "")

            # Cambiar cuenta si hay múltiples (no hace falta en la primera si ya está activa,
            # pero siempre es seguro hacerlo para garantizar la selección correcta)
            if label and multi:
                logger.info("[%s] Cambiando a cuenta '%s'", self.nombre, label)
                # Navegar al dashboard antes de cada cambio de cuenta para garantizar
                # que Angular cargue el header con el md-select de cuentas.
                # Necesario tanto para la primera como para cuentas subsiguientes;
                # sin esto la sesión se degrada y el md-select deja de ser accesible.
                dashboard_url = (
                    self.url_login.split("#")[0]
                    .replace("login.html", "desktop.html")
                    + "#!/estado"
                )
                await page.goto(dashboard_url, wait_until="domcontentloaded", timeout=timeout)
                await asyncio.sleep(3)
                await self._switch_cuenta(label, timeout)

            dest_base = dest_dir / cta_nombre if (multi and cta_nombre) else dest_dir

            # ── 2. Navegar a BOLETOS ──────────────────────────────────────────
            logger.info("[%s] Abriendo sección BOLETOS [%s]",
                        self.nombre, cta_nombre or "default")
            await self._navegar_boletos(timeout)

            # ── 2b. Aplicar filtro de fecha via scope Angular ─────────────────
            await self._aplicar_filtro_fecha(fecha)

            # ── 3. Leer filas de la grilla filtrando por fecha ───────────────
            # Primero scrollear toda la tabla para forzar el lazy-rendering
            # de los íconos PDF (el portal no los renderiza hasta que son visibles)
            await page.evaluate("""
                () => {
                    const rows = document.querySelectorAll('table tr[data-id]');
                    if (rows.length) {
                        rows[rows.length - 1].scrollIntoView();
                        rows[0].scrollIntoView();
                    }
                }
            """)
            await asyncio.sleep(1)
            rows = await self._leer_filas(fecha_fmt)
            logger.info("[%s] Boletos encontrados [%s]: %d",
                        self.nombre, cta_nombre or "default", len(rows))

            if not rows:
                logger.info("[%s] Sin boletos para %s [%s]",
                            self.nombre, fecha, cta_nombre or "default")
                continue

            # Ordenar Cauciones primero — sus íconos desaparecen después de
            # que se descarga algún Pase (re-render del portal)
            rows = sorted(rows, key=lambda r: (0 if self._classify_tipo(r["cells"]) == "Cauciones" else 1))

            # ── 4. Descargar PDFs ─────────────────────────────────────────────
            for row in rows:
                tipo = self._classify_tipo(row["cells"])
                if tipo not in tipos_config:
                    logger.debug("[%s] Omitiendo fila %s (tipo '%s' no configurado)",
                                 self.nombre, row["dataId"], tipo)
                    continue

                dest_tipo_dir = dest_base / tipo
                dest_tipo_dir.mkdir(parents=True, exist_ok=True)
                data_id = row["dataId"]
                logger.info("[%s] Descargando — data-id=%s  tipo=%s [%s]",
                            self.nombre, data_id, tipo, cta_nombre or "default")

                try:
                    # Scrollear el row al viewport — el portal renderiza el ícono PDF
                    # solo cuando el row es visible (lazy rendering)
                    await page.evaluate(f"""
                        () => {{
                            const row = document.querySelector('tr[data-id="{data_id}"]');
                            if (row) row.scrollIntoView({{block: 'center'}});
                        }}
                    """)
                    # Esperar a que el ícono aparezca en el DOM tras el scroll
                    icon_locator = page.locator(f"tr[data-id='{data_id}'] a.icon-file-pdf.app_gridIcon")
                    await icon_locator.wait_for(state="visible", timeout=timeout)
                    # Click en ícono PDF de la fila → abre dialog con link real de descarga
                    await icon_locator.click()
                    await asyncio.sleep(2)

                    # Buscar el link de descarga dentro del dialog.
                    dl_locator = page.locator(
                        ".md-dialog-container a[href*='GetFormBol'],"
                        " .md-dialog-container a[href]:not([href='#'])"
                    ).first
                    await dl_locator.wait_for(state="visible", timeout=10_000)

                    async with page.expect_download(timeout=timeout) as dl_info:
                        await dl_locator.click()
                    dl = await dl_info.value

                    dest_path = dest_tipo_dir / dl.suggested_filename
                    await dl.save_as(dest_path)
                    logger.info("[%s] Guardado: %s (%d bytes)",
                                self.nombre, dest_path.name, dest_path.stat().st_size)
                    downloaded.append(dest_path)

                except Exception as exc:
                    logger.error("[%s] Error en fila %s — %s: %s",
                                 self.nombre, data_id, type(exc).__name__, exc)
                finally:
                    # Cerrar el dialog antes de la próxima fila, pase lo que pase
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(1)

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

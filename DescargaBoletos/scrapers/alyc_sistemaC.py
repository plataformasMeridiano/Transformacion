import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000

_BASE_URL          = "https://clientes.winsa.com.ar"
_URL_PESOS_TIPO    = f"{_BASE_URL}/Consultas/PesosPorTipoOperacion"
_URL_COMPROBANTE   = f"{_BASE_URL}/Consultas/GetComprobante"

# Mapeo nombre config → valor del <select> #idInputTipoCombo1
_TIPO_A_COMBO = {
    "Compra/Venta": "00",
    "Opciones":     "01",
    "Pases":        "02",
    "Cauciones":    "03",
    "Senebi":       "04",
}

# Regex para extraer el ID de href="javascript:getComprobante('ID')"
_RE_ID = re.compile(r"getComprobante\('([^']+)'\)")


class WinScraper(BaseScraper):
    """
    Scraper para el portal WIN Securities (sistemaC — ASP.NET MVC, HTML clásico).

    Flujo de login:
        1. Navegar a url_login (carga completa, sin SPA)
        2. DNI       (input[name='Dni'])
        3. Usuario   (#usuario)
        4. Contraseña (#passwd)
        5. Click en #loginButton (type=submit — dispara POST a /Login/Ingresar)
        6. Esperar a que el servidor redirija fuera de /Login

    Flujo de descarga (download_tickets):
        Por cada cuenta comitente configurada en opciones.cuentas:
        1. Cambiar el comitente activo via JS (#select_comitente select2)
        2. Por cada tipo configurado en opciones.tipo_operacion:
           a. Navegar a /Consultas/PesosPorTipoOperacion
           b. Setear combo tipo = valor correspondiente
           c. Click en "Consultar" y esperar resultados (networkidle)
           d. Filtrar filas por fecha de concertación (cells[2], dd/mm/yy)
           e. Extraer IDs de comprobante de los hrefs: getComprobante('ID')
           f. Para cada ID: POST a /Consultas/GetComprobante con {"clave": ID}
              → responde JSON con Result = "data:application/pdf;base64,..."
           g. Decodificar base64 y guardar PDF en dest_dir/{cuenta}/{tipo}/{ID}.pdf

    Configuración relevante en opciones:
        cuentas (list[dict])  Lista de cuentas comitentes:
                              {"nombre": str, "comitente": str}
                              Si hay >1 entrada, los PDFs se guardan en
                              dest_dir/{cuenta.nombre}/{tipo}/
                              Si está vacío o ausente: usa el comitente por defecto.
        tipo_operacion (list[str])  Tipos a descargar: "Cauciones", "Pases", etc.
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self.documento = self._resolve(alyc_config["documento"])

    async def login(self) -> bool:
        page = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        logger.info("[%s] Navegando a %s", self.nombre, self.url_login)
        await page.goto(self.url_login, wait_until="load", timeout=timeout)

        logger.info("[%s] Completando formulario de login", self.nombre)
        await page.fill("input[name='Dni']", self.documento)
        await page.fill("#usuario", self.usuario)
        await page.fill("#passwd", self.contrasena)

        logger.info("[%s] Enviando credenciales", self.nombre)
        await page.click("#loginButton")

        # El server hace redirect HTTP → esperamos que la URL salga de /Login
        await page.wait_for_url(
            lambda url: "/Login" not in url,
            timeout=timeout,
        )

        logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
        return True

    async def _switch_comitente(self, comitente: str, timeout: int) -> None:
        """
        Cambia el comitente activo via el select2 #select_comitente.
        Navega a la página, abre el dropdown visual del select2 (que carga las
        opciones dinámicamente via AJAX), y hace click en la opción que contiene
        el número de comitente.  Finalmente espera networkidle para confirmar
        que el portal actualizó la sesión en el servidor.
        """
        page = self._page
        await page.goto(_URL_PESOS_TIPO, wait_until="load", timeout=timeout)

        # Abrir el dropdown visual del select2
        await page.click(".select2-container .select2-selection")
        # Esperar a que aparezcan las opciones (carga dinámica)
        await page.wait_for_selector(".select2-results__option", timeout=timeout)

        # Hacer click en la opción que corresponde al comitente
        await page.locator(
            ".select2-results__option",
            has_text=comitente,
        ).first.click()

        await page.wait_for_load_state("networkidle", timeout=timeout)
        # El portal renderiza la tabla en dos pasos: networkidle captura el
        # primer AJAX pero el contenido real puede llegar ~3s después.
        await page.wait_for_timeout(3000)

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga los comprobantes PDF de la fecha indicada (YYYY-MM-DD).

        Con múltiples cuentas configuradas, la estructura de destino es:
            dest_dir/
            ├── MeridianoNorte/
            │   ├── Cauciones/
            │   │   └── 1050015C6XXXXXXXX.pdf
            │   └── Pases/
            │       └── 1050015C6XXXXXXXX.pdf
            └── Pamat/
                └── Cauciones/
                    └── 1050017C6XXXXXXXX.pdf

        Con una sola cuenta (o sin `cuentas` en opciones), estructura plana:
            dest_dir/
            ├── Cauciones/
            └── Pases/
        """
        page         = self._page
        timeout      = self.opciones.get("timeout_ms", _TIMEOUT)
        tipos_config: list[str]  = self.opciones.get("tipo_operacion", [])
        cuentas: list[dict]      = self.opciones.get("cuentas", [{}])
        fecha_dt     = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_fmt2y  = fecha_dt.strftime("%d/%m/%y")

        # Usar subcarpeta por cuenta solo cuando hay múltiples
        multi = len(cuentas) > 1 and any(c.get("nombre") for c in cuentas)

        downloaded: list[Path] = []

        for cuenta in cuentas:
            comitente  = cuenta.get("comitente", "")
            cta_nombre = cuenta.get("nombre", "")
            dest_base = dest_dir / cta_nombre if (multi and cta_nombre) else dest_dir

            for tipo in tipos_config:
                combo_val = _TIPO_A_COMBO.get(tipo)
                if combo_val is None:
                    logger.warning("[%s] Tipo '%s' desconocido — omitiendo", self.nombre, tipo)
                    continue

                # ── 1. Navegar y aplicar filtro ──────────────────────────────────
                logger.info("[%s] Consultando %s [%s] — fecha %s",
                            self.nombre, tipo, cta_nombre or "default", fecha)
                # Navegamos vía _switch_comitente (que ya hace goto) para que el
                # comitente quede establecido en sesión ANTES de aplicar el filtro
                # de tipo. Si lo hiciéramos al inicio del loop de cuentas, el
                # page.goto() de aquí abajo resetearía el comitente al default.
                if comitente:
                    logger.info("[%s] Cambiando comitente a %s (%s)", self.nombre, comitente, cta_nombre)
                    await self._switch_comitente(comitente, timeout)
                else:
                    await page.goto(_URL_PESOS_TIPO, wait_until="load", timeout=timeout)

                await page.select_option("#idInputTipoCombo1", value=combo_val)
                await page.click("button.boton-consulta")
                await page.wait_for_load_state("networkidle", timeout=timeout)
                await page.wait_for_timeout(3000)

                # ── 2. Extraer IDs de comprobante y N° Ope ────────────────────────
                rows = await page.evaluate(f"""
                    () => [...document.querySelectorAll('table tbody tr')]
                        .filter(tr => {{
                            const tds = [...tr.querySelectorAll('td')]
                                            .map(td => td.innerText.trim());
                            return tds.length >= 5 && tds[2] === '{fecha_fmt2y}';
                        }})
                        .map(tr => {{
                            const tds = [...tr.querySelectorAll('td')]
                                            .map(td => td.innerText.trim());
                            const href = tr.querySelector('a[href*="getComprobante"]')
                                           ?.getAttribute('href');
                            // col 4 = "N° Ope." — ej. "11.454" → "11454"
                            const nro = (tds[4] || '').replace(/\\./g, '');
                            return href ? {{href, nro}} : null;
                        }})
                        .filter(Boolean)
                """)
                comps = [(m.group(1), r["nro"]) for r in rows if (m := _RE_ID.search(r["href"]))]
                logger.info("[%s] %s [%s] — %d comprobantes encontrados",
                            self.nombre, tipo, cta_nombre or "default", len(comps))

                if not comps:
                    continue

                dest_tipo_dir = dest_base / tipo
                dest_tipo_dir.mkdir(parents=True, exist_ok=True)

                # ── 3. Descargar cada PDF via POST ────────────────────────────────
                for comp_id, nro_ope in comps:
                    logger.info("[%s] Descargando %s/%s (N°%s) [%s]",
                                self.nombre, tipo, comp_id, nro_ope, cta_nombre or "default")
                    try:
                        resp = await page.context.request.post(
                            _URL_COMPROBANTE,
                            data=json.dumps({"clave": comp_id}),
                            headers={"Content-Type": "application/json"},
                            timeout=timeout,
                        )
                        payload = await resp.json()

                        if not payload.get("Success"):
                            logger.error("[%s] GetComprobante falló para %s: %s",
                                         self.nombre, comp_id, payload.get("Error"))
                            continue

                        # Result = "data:application/pdf;base64,<b64>"
                        b64_content = payload["Result"].split(",", 1)[1]
                        pdf_bytes   = base64.b64decode(b64_content)

                        dest_path = dest_tipo_dir / f"{nro_ope}.pdf"
                        dest_path.write_bytes(pdf_bytes)
                        logger.info("[%s] Guardado: %s (%d bytes)",
                                    self.nombre, dest_path.name, len(pdf_bytes))
                        downloaded.append(dest_path)

                    except Exception as exc:
                        logger.error("[%s] Error en %s — %s: %s",
                                     self.nombre, comp_id, type(exc).__name__, exc)

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

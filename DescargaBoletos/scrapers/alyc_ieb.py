"""scrapers/alyc_ieb.py — Scraper para IEB (clientesv2.invertirenbolsa.com.ar)."""
import base64
import json
import logging
from datetime import datetime
from pathlib import Path

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000
_BASE    = "https://clientesv2.invertirenbolsa.com.ar"


class IEBScraper(BaseScraper):
    """
    Scraper para IEB (clientesv2.invertirenbolsa.com.ar).
    Portal ASP.NET MVC con jQuery AJAX.

    Login: 3 campos (Dni, Usuario, Password) → POST / → /Consultas/PortafolioOnline.

    Flujo de descarga (download_tickets):
        1. Navegar a /Consultas/CuentaCorrientePesos (establece contexto de sesión).
        2. POST /Consultas/GetConsulta con proceso=02, comitente, fechaDesde, fechaHasta.
        3. Filtrar: CPTE="VCMV" y ESPE contiene "FACTURA ELECTRONICA" → ventas de FCE.
        4. Por cada FCE: POST /Consultas/GetComprobante con {clave: item["CLAV"]}.
        5. Response.Result = "data:application/pdf;base64,..." → decodificar y guardar.

    Configuración en opciones:
        comitente      (str)       Código de comitente. Default: "365533".
        tipo_operacion (list[str]) Tipos a descargar. Default: ["Venta FCE-eCheq"].
        timeout_ms     (int)       Timeout en ms. Default: 30000.
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self._documento = self._resolve(alyc_config.get("documento", ""))
        self._comitente = self.opciones.get("comitente", "365533")

    async def _fetch_post(self, url: str, body: dict) -> dict:
        result = await self._page.evaluate(
            """async ([url, bodyStr]) => {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json; charset=utf-8'},
                    body: bodyStr,
                    credentials: 'include'
                });
                return await r.json();
            }""",
            [url, json.dumps(body)],
        )
        return result

    async def login(self) -> bool:
        page    = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        logger.info("[%s] Navegando a %s", self.nombre, self.url_login)
        await page.goto(self.url_login, wait_until="networkidle", timeout=timeout)

        await page.fill('input[name="Dni"]',      self._documento)
        await page.fill('input[name="Usuario"]',  self.usuario)
        await page.fill('input[name="Password"]', self.contrasena)
        await page.click('input[type="submit"]')
        await page.wait_for_load_state("networkidle", timeout=timeout)

        if "Consultas" in page.url or "Portafolio" in page.url:
            logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
            return True

        logger.error("[%s] Login fallido — URL: %s", self.nombre, page.url)
        return False

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga comprobantes FCE del día indicado (formato YYYY-MM-DD).
        Guarda en dest_dir/Venta FCE-eCheq/{NroComprobante}.pdf
        """
        timeout      = self.opciones.get("timeout_ms", _TIMEOUT)
        tipos_config = self.opciones.get("tipo_operacion", ["Venta FCE-eCheq"])

        if "Venta FCE-eCheq" not in tipos_config:
            return []

        fecha_dt  = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_fmt = fecha_dt.strftime("%d/%m/%Y")

        # Establecer contexto de sesión
        await self._page.goto(
            f"{_BASE}/Consultas/CuentaCorrientePesos",
            wait_until="networkidle", timeout=timeout,
        )

        logger.info("[%s] Consultando CC para %s", self.nombre, fecha)
        cc_resp = await self._fetch_post(
            f"{_BASE}/Consultas/GetConsulta",
            {
                "comitente":    self._comitente,
                "consolida":    "0",
                "proceso":      "02",
                "fechaDesde":   fecha_fmt,
                "fechaHasta":   fecha_fmt,
                "tipo":         None,
                "especie":      None,
                "comitenteMana": None,
            },
        )

        if not cc_resp.get("Success"):
            logger.error("[%s] GetConsulta falló: %s", self.nombre, cc_resp.get("Error"))
            return []

        movimientos = cc_resp.get("Result", {}).get("Detalle", [])
        logger.info("[%s] Movimientos el %s: %d", self.nombre, fecha, len(movimientos))

        fce_movs = [
            m for m in movimientos
            if m.get("CPTE") == "VCMV"
            and "FACTURA ELECTRONICA" in (m.get("ESPE") or "").upper()
        ]

        if not fce_movs:
            logger.info("[%s] Sin ventas FCE para %s", self.nombre, fecha)
            return []

        logger.info("[%s] Ventas FCE el %s: %d", self.nombre, fecha, len(fce_movs))

        dest_tipo_dir = dest_dir / "Venta FCE-eCheq"
        dest_tipo_dir.mkdir(parents=True, exist_ok=True)

        downloaded: list[Path] = []

        for m in fce_movs:
            clav    = m.get("CLAV", "")
            nro     = str(m.get("NroComprobante", "")).strip()
            importe = m.get("IMPO", "")

            if not clav or not nro:
                logger.warning("[%s] Movimiento sin CLAV/NroComprobante: %s", self.nombre, m)
                continue

            dest_file = dest_tipo_dir / f"{nro}.pdf"
            if dest_file.exists():
                logger.info("[%s] Ya existe: %s", self.nombre, dest_file.name)
                downloaded.append(dest_file)
                continue

            logger.info("[%s] Descargando FCE nro=%s CLAV=%s importe=%s",
                        self.nombre, nro, clav, importe)

            try:
                cte_resp = await self._fetch_post(
                    f"{_BASE}/Consultas/GetComprobante",
                    {"clave": clav},
                )

                if not cte_resp.get("Success"):
                    logger.error("[%s] GetComprobante falló nro=%s: %s",
                                 self.nombre, nro, cte_resp.get("Error"))
                    continue

                result_str = cte_resp.get("Result", "")
                if not result_str or not result_str.startswith("data:application/pdf;base64,"):
                    logger.error("[%s] Respuesta inesperada GetComprobante nro=%s: %s",
                                 self.nombre, nro, str(result_str)[:100])
                    continue

                pdf_bytes = base64.b64decode(result_str.split(",", 1)[1])
                if pdf_bytes[:4] != b"%PDF":
                    logger.error("[%s] No es PDF — nro=%s", self.nombre, nro)
                    continue

                dest_file.write_bytes(pdf_bytes)
                logger.info("[%s] Guardado: %s (%d bytes)",
                            self.nombre, dest_file.name, len(pdf_bytes))
                downloaded.append(dest_file)

            except Exception as exc:
                logger.error("[%s] Error descargando nro=%s: %s: %s",
                             self.nombre, nro, type(exc).__name__, exc)

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

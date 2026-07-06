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

# CPTE codes por defecto (descubiertos vía diagnóstica)
_DEFAULT_CAUCION_CODES    = frozenset({"TCC", "TOCT"})   # tomadoras ARS: apertura + cierre
_DEFAULT_COLOCAD_CODES    = frozenset()
_DEFAULT_PASE_CODES       = frozenset()


class IEBScraper(BaseScraper):
    """
    Scraper para IEB (clientesv2.invertirenbolsa.com.ar).
    Portal ASP.NET MVC con jQuery AJAX.

    Login: 3 campos (Dni, Usuario, Password) → POST / → /Consultas/PortafolioOnline.

    Todos los tipos de operación se obtienen del mismo endpoint:
        POST /Consultas/GetConsulta  proceso=02  → /Consultas/CuentaCorrientePesos
        → Result.Detalle[]: {CPTE, ESPE, CLAV, NroComprobante, FEC1, IMPO, ...}

    Clasificación por CPTE:
        "VCMV" + ESPE="FACTURA ELECTRONICA" → Venta FCE-eCheq
        "TCC", "TOCT"                        → Cauciones  (apertura + cierre tomadora)
        caucion_codes (config)               → Cauciones Colocadoras
        pase_codes (config)                  → Pases

    Descarga: POST /Consultas/GetComprobante {clave: CLAV}
        → Result = "data:application/pdf;base64,..."

    Configuración en opciones:
        comitente          (str)       Código de comitente. Default: "365533".
        tipo_operacion     (list[str]) Tipos a descargar.
        caucion_codes      (list[str]) CPTE codes para Cauciones.
                                       Default: ["TCC", "TOCT"].
        colocadoras_codes  (list[str]) CPTE codes para Cauciones Colocadoras.
                                       Default: [].
        pase_codes         (list[str]) CPTE codes para Pases. Default: [].
        timeout_ms         (int)       Timeout en ms. Default: 30000.
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self._documento = self._resolve(alyc_config.get("documento", ""))
        self._comitente = self.opciones.get("comitente", "365533")

        cau  = self.opciones.get("caucion_codes")
        col  = self.opciones.get("colocadoras_codes")
        pase = self.opciones.get("pase_codes")
        self._caucion_codes = frozenset(c.upper() for c in cau)  if cau  is not None else _DEFAULT_CAUCION_CODES
        self._colocad_codes = frozenset(c.upper() for c in col)  if col  is not None else _DEFAULT_COLOCAD_CODES
        self._pase_codes    = frozenset(c.upper() for c in pase) if pase is not None else _DEFAULT_PASE_CODES

    def _classify_cc(self, cpte: str, m: dict) -> str | None:
        """Clasifica un item de CuentaCorriente por su CPTE. Retorna tipo o None."""
        cpte_up = cpte.upper()
        if cpte_up == "VCMV" and "FACTURA ELECTRONICA" in (m.get("ESPE") or "").upper():
            return "Venta FCE-eCheq"
        if cpte_up in self._caucion_codes:
            return "Cauciones"
        if cpte_up in self._colocad_codes:
            return "Cauciones Colocadoras"
        if cpte_up in self._pase_codes:
            return "Pases"
        return None

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

    async def _download_comprobante(self, clav: str, nro: str) -> bytes | None:
        """Descarga PDF via GetComprobante. Retorna bytes o None."""
        try:
            cte_resp = await self._fetch_post(
                f"{_BASE}/Consultas/GetComprobante",
                {"clave": clav},
            )
            if not cte_resp.get("Success"):
                logger.error("[%s] GetComprobante falló nro=%s: %s",
                             self.nombre, nro, cte_resp.get("Error"))
                return None

            result_str = cte_resp.get("Result", "")
            if not result_str:
                logger.error("[%s] Respuesta vacía GetComprobante nro=%s", self.nombre, nro)
                return None

            if result_str.startswith("data:application/pdf;base64,"):
                pdf_bytes = base64.b64decode(result_str.split(",", 1)[1])
            elif result_str.startswith("/") or result_str.startswith("http"):
                pdf_url = (_BASE + result_str) if result_str.startswith("/") else result_str
                b64 = await self._page.evaluate(
                    """async ([url]) => {
                        const r = await fetch(url, {credentials: 'include'});
                        const ab = await r.arrayBuffer();
                        const b = new Uint8Array(ab);
                        let s = '';
                        for (let i = 0; i < b.byteLength; i++) s += String.fromCharCode(b[i]);
                        return btoa(s);
                    }""",
                    [pdf_url],
                )
                pdf_bytes = base64.b64decode(b64)
            else:
                logger.error("[%s] Formato inesperado GetComprobante nro=%s: %s",
                             self.nombre, nro, str(result_str)[:100])
                return None

            if pdf_bytes[:4] != b"%PDF":
                logger.error("[%s] No es PDF — nro=%s", self.nombre, nro)
                return None
            return pdf_bytes

        except Exception as exc:
            logger.error("[%s] Error GetComprobante nro=%s: %s: %s",
                         self.nombre, nro, type(exc).__name__, exc)
            return None

    async def login(self) -> bool:
        page    = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        logger.info("[%s] Navegando a %s", self.nombre, self.url_login)
        await page.goto(self.url_login, wait_until="load", timeout=timeout)

        await page.fill('input[name="Dni"]',      self._documento)
        await page.fill('input[name="Usuario"]',  self.usuario)
        await page.fill('input[name="Password"]', self.contrasena)
        await page.click('input[type="submit"]')
        await page.wait_for_load_state("load", timeout=timeout)

        if "Consultas" in page.url or "Portafolio" in page.url:
            logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
            return True

        logger.error("[%s] Login fallido — URL: %s", self.nombre, page.url)
        return False

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """Descarga comprobantes del día indicado (YYYY-MM-DD) desde CuentaCorrientePesos."""
        timeout      = self.opciones.get("timeout_ms", _TIMEOUT)
        tipos_config = self.opciones.get("tipo_operacion", ["Venta FCE-eCheq"])

        fecha_dt   = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_fmt  = fecha_dt.strftime("%d/%m/%Y")
        # FEC1 en formato "DD/MM/YY" — año de 2 dígitos
        fecha_fec1 = fecha_dt.strftime("%d/%m/%y")

        downloaded: list[Path] = []

        # Establecer contexto de sesión en CuentaCorrientePesos
        await self._page.goto(
            f"{_BASE}/Consultas/CuentaCorrientePesos",
            wait_until="load", timeout=timeout,
        )

        logger.info("[%s] Consultando CC para %s", self.nombre, fecha)
        cc_resp = await self._fetch_post(
            f"{_BASE}/Consultas/GetConsulta",
            {
                "comitente": self._comitente, "consolida": "0", "proceso": "02",
                "fechaDesde": fecha_fmt, "fechaHasta": fecha_fmt,
                "tipo": None, "especie": None, "comitenteMana": None,
            },
        )

        if not cc_resp.get("Success"):
            logger.error("[%s] GetConsulta falló: %s", self.nombre, cc_resp.get("Error"))
            return []

        movimientos = cc_resp.get("Result", {}).get("Detalle", [])
        # FEC1 filtra por fecha real de operación (el API acumula datos de todo el período)
        movs_dia = [m for m in movimientos if m.get("FEC1") == fecha_fec1]
        logger.info("[%s] Movimientos el %s: %d", self.nombre, fecha, len(movs_dia))

        for m in movs_dia:
            cpte = m.get("CPTE", "")
            tipo = self._classify_cc(cpte, m)

            if tipo is None:
                logger.debug("[%s] CPTE=%s ESPE=%s — sin clasificar, omitiendo",
                             self.nombre, cpte, m.get("ESPE"))
                continue
            if tipo not in tipos_config:
                logger.debug("[%s] CPTE=%s tipo='%s' no configurado", self.nombre, cpte, tipo)
                continue

            clav = m.get("CLAV", "")
            nro  = str(m.get("NroComprobante", "")).strip()
            if not clav or not nro:
                logger.warning("[%s] CPTE=%s sin CLAV/NroComprobante: %s", self.nombre, cpte, m)
                continue

            dest_tipo_dir = dest_dir / tipo
            dest_tipo_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_tipo_dir / f"{nro}.pdf"

            if dest_file.exists():
                logger.info("[%s] Ya existe: %s", self.nombre, dest_file.name)
                downloaded.append(dest_file)
                continue

            logger.info("[%s] Descargando %s nro=%s CPTE=%s CLAV=%s importe=%s",
                        self.nombre, tipo, nro, cpte, clav, m.get("IMPO", ""))
            pdf_bytes = await self._download_comprobante(clav, nro)
            if pdf_bytes is not None:
                dest_file.write_bytes(pdf_bytes)
                logger.info("[%s] Guardado: %s (%d bytes)",
                            self.nombre, dest_file.name, len(pdf_bytes))
                downloaded.append(dest_file)

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

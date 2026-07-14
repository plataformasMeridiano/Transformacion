"""scrapers/alyc_ieb.py — Scraper para IEB (clientesv2.invertirenbolsa.com.ar)."""
import base64
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000
_BASE    = "https://clientesv2.invertirenbolsa.com.ar"

# Procesos de GetConsulta
_PROC_OPERACIONES_DIA = "05"   # operaciones por fecha de CONCERTACIÓN (identificación)
_PROC_CUENTA_CTE      = "02"   # cuenta corriente / ledger (para la CLAV de descarga)

# Ventana forward (días corridos) al consultar proceso=02 para armar el mapa
# NUME→CLAV. El ledger publica el boleto de la pata de cierre (TOCT) con su
# CLAV ya al concertar, pero fechado en su fecha de liquidación; hay que abarcar
# hasta esa fecha. Las cauciones/pases pueden ser a plazo largo (30, 60, 90 días),
# así que se usa una ventana amplia para no perder cierres de plazo extendido.
_CLAV_WINDOW_DAYS = 120

# CPTE codes por defecto (descubiertos vía diagnóstica)
_DEFAULT_CAUCION_CODES    = frozenset({"TCC", "TOCT"})   # tomadoras ARS: apertura + cierre
_DEFAULT_COLOCAD_CODES    = frozenset()
_DEFAULT_PASE_CODES       = frozenset()
_DEFAULT_TITULOS_CODES    = frozenset()   # ej. {"VRCN", "CRCN"} — configurable


class IEBScraper(BaseScraper):
    """
    Scraper para IEB (clientesv2.invertirenbolsa.com.ar).
    Portal ASP.NET MVC con jQuery AJAX.

    Login: 3 campos (Dni, Usuario, Password) → POST / → /Consultas/PortafolioOnline.

    Descarga en dos pasos (identificar → descargar):

      1. IDENTIFICAR por fecha de CONCERTACIÓN:
         POST /Consultas/GetConsulta  proceso=05  (OperacionesDia)
         → Result.Operaciones[]: agrupado por especie; cada uno con Detalle[] de
           patas {CPTE, ESPE, Comprobante, NUME, CLAVE, IMPO, ...}.
         Esta vista lista TODAS las patas de la operación bajo su día de
         concertación (apertura + cierre de caución juntos, aunque el cierre
         liquide en fecha futura), y también títulos y FCE-eCheq del día.

      2. DESCARGAR: la CLAVE de proceso=05 NO sirve para GetComprobante; la clave
         de descarga válida es la CLAV de proceso=02 (CuentaCorrientePesos).
         Se arma un mapa NUME→CLAV con proceso=02 sobre una ventana forward
         (para capturar la CLAV del cierre, que se publica en su liquidación) y
         se descarga cada boleto:
         POST /Consultas/GetComprobante {clave: CLAV}
             → Result = "data:application/pdf;base64,..."

    Clasificación por CPTE (sobre las patas de proceso=05):
        "VCMV" + Nombre contiene "FACTURA ELECTRONICA" → Venta FCE-eCheq
        caucion_codes     (default TCC, TOCT)          → Cauciones
        colocadoras_codes (config)                     → Cauciones Colocadoras
        pase_codes        (config)                     → Pases
        titulos_codes     (config, ej. VRCN/CRCN)      → Títulos

    Todo se archiva bajo `fecha` (= concertación), sin depender de FEC1/FEC2.

    Configuración en opciones:
        comitente          (str)       Código de comitente. Default: "365533".
        tipo_operacion     (list[str]) Tipos a descargar.
        caucion_codes      (list[str]) CPTE codes para Cauciones.
                                       Default: ["TCC", "TOCT"].
        colocadoras_codes  (list[str]) CPTE codes para Cauciones Colocadoras.
                                       Default: [].
        pase_codes         (list[str]) CPTE codes para Pases. Default: [].
        titulos_codes      (list[str]) CPTE codes para Títulos. Default: [].
        timeout_ms         (int)       Timeout en ms. Default: 30000.
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self._documento = self._resolve(alyc_config.get("documento", ""))
        self._comitente = self.opciones.get("comitente", "365533")

        cau  = self.opciones.get("caucion_codes")
        col  = self.opciones.get("colocadoras_codes")
        pase = self.opciones.get("pase_codes")
        tit  = self.opciones.get("titulos_codes")
        self._caucion_codes = frozenset(c.upper() for c in cau)  if cau  is not None else _DEFAULT_CAUCION_CODES
        self._colocad_codes = frozenset(c.upper() for c in col)  if col  is not None else _DEFAULT_COLOCAD_CODES
        self._pase_codes    = frozenset(c.upper() for c in pase) if pase is not None else _DEFAULT_PASE_CODES
        self._titulos_codes = frozenset(c.upper() for c in tit)  if tit  is not None else _DEFAULT_TITULOS_CODES

    def _classify_leg(self, leg: dict, nombre: str) -> str | None:
        """Clasifica una pata de proceso=05 por su CPTE. Retorna tipo o None.

        `leg`    es un item de Operaciones[].Detalle[] (con CPTE, ESPE, ...).
        `nombre` es el Nombre de la operación agrupadora (especie/instrumento),
                 usado para distinguir FCE-eCheq (el CPTE VCMV también se usa en
                 otras ventas MAV).
        """
        cpte_up  = (leg.get("CPTE") or "").upper()
        nombre_up = (nombre or "").upper()
        if cpte_up == "VCMV" and "FACTURA ELECTRONICA" in nombre_up:
            return "Venta FCE-eCheq"
        if cpte_up in self._caucion_codes:
            return "Cauciones"
        if cpte_up in self._colocad_codes:
            return "Cauciones Colocadoras"
        if cpte_up in self._pase_codes:
            return "Pases"
        if cpte_up in self._titulos_codes:
            return "Títulos"
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

    async def _clav_por_nume(self, fecha_dt: datetime, timeout: int) -> dict[str, str]:
        """Mapa NroComprobante(str) → CLAV de descarga, vía proceso=02.

        Consulta una ventana forward para incluir las patas de cierre (TOCT),
        que recién se publican en el ledger en su fecha de liquidación.
        """
        desde = fecha_dt.strftime("%d/%m/%Y")
        hasta = (fecha_dt + timedelta(days=_CLAV_WINDOW_DAYS)).strftime("%d/%m/%Y")
        cc = await self._fetch_post(
            f"{_BASE}/Consultas/GetConsulta",
            {
                "comitente": self._comitente, "consolida": "0",
                "proceso": _PROC_CUENTA_CTE,
                "fechaDesde": desde, "fechaHasta": hasta,
                "tipo": None, "especie": None, "comitenteMana": None,
            },
        )
        if not cc.get("Success"):
            logger.error("[%s] GetConsulta(02) falló: %s", self.nombre, cc.get("Error"))
            return {}
        detalle = cc.get("Result", {}).get("Detalle", [])
        return {
            str(m.get("NroComprobante", "")).strip(): m.get("CLAV")
            for m in detalle if m.get("CLAV")
        }

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """Descarga los comprobantes CONCERTADOS el día indicado (YYYY-MM-DD).

        Usa OperacionesDia (proceso=05) para identificar los boletos por fecha de
        concertación — incluye ambas patas de cada caución (apertura + cierre) el
        mismo día — y la CLAV de proceso=02 para descargarlos.
        """
        timeout      = self.opciones.get("timeout_ms", _TIMEOUT)
        tipos_config = self.opciones.get("tipo_operacion", ["Venta FCE-eCheq"])

        fecha_dt  = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_fmt = fecha_dt.strftime("%d/%m/%Y")

        downloaded: list[Path] = []

        # Establecer contexto de sesión en OperacionesDia
        await self._page.goto(
            f"{_BASE}/Consultas/OperacionesDia",
            wait_until="load", timeout=timeout,
        )

        # ── 1. IDENTIFICAR por concertación (proceso=05) ────────────────────────
        logger.info("[%s] Consultando OperacionesDia (concertación) %s", self.nombre, fecha)
        op_resp = await self._fetch_post(
            f"{_BASE}/Consultas/GetConsulta",
            {
                "comitente": self._comitente, "consolida": "0",
                "proceso": _PROC_OPERACIONES_DIA,
                "fechaDesde": fecha_fmt, "tipo": None,
            },
        )
        if not op_resp.get("Success"):
            logger.error("[%s] GetConsulta(05) falló: %s", self.nombre, op_resp.get("Error"))
            return []

        operaciones = op_resp.get("Result", {}).get("Operaciones", []) or []
        # Aplanar patas clasificables: (nume, tipo, cpte)
        patas: list[tuple[str, str, str]] = []
        for op in operaciones:
            nombre = op.get("Nombre", "")
            for leg in op.get("Detalle", []) or []:
                tipo = self._classify_leg(leg, nombre)
                if tipo is None:
                    logger.debug("[%s] CPTE=%s (%s) sin clasificar, omitiendo",
                                 self.nombre, leg.get("CPTE"), nombre.strip()[:30])
                    continue
                if tipo not in tipos_config:
                    logger.debug("[%s] CPTE=%s tipo='%s' no configurado",
                                 self.nombre, leg.get("CPTE"), tipo)
                    continue
                nume = str(leg.get("NUME", "")).strip()
                if not nume:
                    logger.warning("[%s] pata sin NUME: %s", self.nombre, leg)
                    continue
                patas.append((nume, tipo, (leg.get("CPTE") or "").upper()))

        logger.info("[%s] Boletos concertados el %s (clasificados/configurados): %d",
                    self.nombre, fecha, len(patas))
        if not patas:
            return []

        # ── 2. CLAV de descarga (proceso=02, ventana forward) ───────────────────
        clav_by_nume = await self._clav_por_nume(fecha_dt, timeout)

        # ── 3. DESCARGAR ────────────────────────────────────────────────────────
        for nume, tipo, cpte in patas:
            dest_tipo_dir = dest_dir / tipo
            dest_file = dest_tipo_dir / f"{nume}.pdf"
            if dest_file.exists():
                logger.info("[%s] Ya existe: %s", self.nombre, dest_file.name)
                downloaded.append(dest_file)
                continue

            clav = clav_by_nume.get(nume)
            if not clav:
                # típicamente el cierre (TOCT) aún no liquidó → sin CLAV en el ledger;
                # se descargará en una corrida posterior (dentro de la ventana).
                logger.warning("[%s] Boleto %s (%s/%s) sin CLAV disponible aún — se reintentará",
                               self.nombre, nume, tipo, cpte)
                continue

            dest_tipo_dir.mkdir(parents=True, exist_ok=True)
            logger.info("[%s] Descargando %s nro=%s CPTE=%s CLAV=%s",
                        self.nombre, tipo, nume, cpte, clav)
            pdf_bytes = await self._download_comprobante(clav, nume)
            if pdf_bytes is not None:
                dest_file.write_bytes(pdf_bytes)
                logger.info("[%s] Guardado: %s (%d bytes)",
                            self.nombre, dest_file.name, len(pdf_bytes))
                downloaded.append(dest_file)

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

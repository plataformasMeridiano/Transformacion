import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_TIMEOUT = 30_000
_URL_SESSION = "https://virtualbroker-conosur.aunesa.com/api/auth/session"
_API_BASE = "https://vb-back-conosur.aunesa.com/api"

# Conceptos que identifican cauciones (columna 'concepto' de la API)
_DEFAULT_CAUCION_CONCEPTOS = frozenset({"TOMADORA", "COLOCADORA"})


class ConoSurScraper(BaseScraper):
    """
    Scraper para Virtual Broker ConoSur (Next.js + React + Ant Design).

    Flujo de login:
        1. Navegar a /auth/signin y esperar networkidle
        2. Usuario  (#usuario)   — usar keyboard.type() (fill() no dispara React onChange)
        3. Clave    (#contraseña) — ídem
        4. Click en button[type='submit']
        5. Esperar a que la URL no contenga /auth/signin

    Flujo de descarga (download_tickets):
        1. Obtener JWT desde GET /api/auth/session → accessToken
        2. GET /api/v2/cuentas/{cuenta}/movimientos con rango ampliado (fecha + 7 días)
           para capturar la pata de liquidación de cauciones que cae al día siguiente
        3. Filtrar client-side por concertacion == fecha
        4. Por cada movimiento: GET /api/comprobantes/{quote(nro)}?formato=PDF
        5. Guardar como dest_dir/{tipo}/{nro}.pdf

    Configuración relevante en opciones:
        cuenta              (str)       Número de cuenta. Obligatorio (ej. "3003").
        caucion_conceptos   (list[str]) Conceptos que identifican cauciones.
                                        Default: ["Tomadora", "Colocadora"].
        tipo_operacion      (list[str]) Subtipos a descargar: "Cauciones", "Pases".
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        super().__init__(alyc_config, general_config)
        self._cuenta = self.opciones["cuenta"]
        conceptos = self.opciones.get("caucion_conceptos")
        self._caucion_conceptos = (
            frozenset(c.upper() for c in conceptos)
            if conceptos is not None
            else _DEFAULT_CAUCION_CONCEPTOS
        )

    def _classify_tipo(self, concepto: str) -> str:
        """Clasifica un boleto como 'Cauciones' o 'Pases' según el concepto de la API."""
        if concepto.strip().upper() in self._caucion_conceptos:
            return "Cauciones"
        return "Pases"

    def _match_pases(self, all_movs: list[dict], fecha_fmt: str) -> list[dict]:
        """
        Identifica los pares de pases bursátiles para la fecha dada.

        Un pase consta de dos patas con el mismo simboloLocal:
          - Apertura: Venta concertada en fecha (cualquier liquidación)
          - Cierre:   Compra con liquidación == siguiente día hábil de fecha

        Esto excluye licitaciones, rescates de FCI y compras no relacionadas
        que también tienen concertacion == fecha pero distinto simbolo o liquidación.
        """
        ventas = [
            m for m in all_movs
            if m.get("concepto") == "Venta"
            and m.get("concertacion") == fecha_fmt
            and m.get("simboloLocal")
        ]
        if not ventas:
            return []

        simbolos = {m["simboloLocal"] for m in ventas}

        # Siguiente día hábil (saltear sábado=5 y domingo=6)
        fecha_dt = datetime.strptime(fecha_fmt, "%d/%m/%Y")
        next_biz = fecha_dt + timedelta(days=1)
        while next_biz.weekday() >= 5:
            next_biz += timedelta(days=1)
        next_fmt = next_biz.strftime("%d/%m/%Y")

        compras = [
            m for m in all_movs
            if m.get("concepto") == "Compra"
            and m.get("simboloLocal") in simbolos
            and m.get("liquidacion") == next_fmt
        ]

        seen: set[str] = set()
        result: list[dict] = []
        for m in ventas + compras:
            nro = m.get("numeroComprobante", "")
            if nro and nro not in seen:
                seen.add(nro)
                result.append(m)
        return result

    async def login(self) -> bool:
        page = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)

        logger.info("[%s] Navegando a %s", self.nombre, self.url_login)
        await page.goto(self.url_login, wait_until="networkidle", timeout=timeout)

        logger.info("[%s] Completando formulario de login", self.nombre)
        await page.click("#usuario")
        await page.keyboard.type(self.usuario, delay=40)
        await page.click("#contraseña")
        await page.keyboard.type(self.contrasena, delay=40)

        logger.info("[%s] Enviando credenciales", self.nombre)
        await page.locator("button[type='submit']").click()
        await page.wait_for_url(lambda u: "/auth/signin" not in u, timeout=20_000)

        logger.info("[%s] Login exitoso — URL: %s", self.nombre, page.url)
        return True

    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga los boletos de la fecha indicada (YYYY-MM-DD).

        Estructura de destino:
            dest_dir/
            ├── Cauciones/
            │   └── BOL_2026067408.pdf
            └── Pases/
                └── BOL_2026067124.pdf

        Solo descarga los tipos configurados en opciones.tipo_operacion.
        Usa la API REST directamente (sin interacción UI tras el login).
        """
        page = self._page
        timeout = self.opciones.get("timeout_ms", _TIMEOUT)
        tipos_config: list[str] = self.opciones.get("tipo_operacion", [])

        fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_fmt = fecha_dt.strftime("%d/%m/%Y")
        fecha_hasta_fmt = (fecha_dt + timedelta(days=7)).strftime("%d/%m/%Y")

        # ── 1. Obtener JWT ────────────────────────────────────────────────
        sess_resp = await page.context.request.get(_URL_SESSION)
        sess = await sess_resp.json()
        auth_h = {"Authorization": f"Bearer {sess['accessToken']}"}
        logger.info("[%s] JWT obtenido", self.nombre)

        # ── 2. Obtener movimientos ─────────────────────────────────────────
        resp = await page.context.request.get(
            f"{_API_BASE}/v2/cuentas/{self._cuenta}/movimientos",
            params={
                "fechaDesde": fecha_fmt,
                "fechaHasta": fecha_hasta_fmt,
                "tipoMovimiento": "monetarios",
                "page": "1",
                "size": "500",
                "estado": "DIS",
                "especie": "ARS",
            },
            headers=auth_h,
        )
        data = await resp.json()
        all_movs = data.get("movimientos", {}).get("content", [])
        logger.info(
            "[%s] Movimientos en rango %s-%s: %d",
            self.nombre, fecha_fmt, fecha_hasta_fmt, len(all_movs),
        )

        # ── 3. Seleccionar movimientos a descargar ─────────────────────────
        # Cauciones: ambas patas tienen concertación == fecha
        movs_cauciones = [
            m for m in all_movs
            if m.get("concertacion") == fecha_fmt
            and self._classify_tipo(m.get("concepto", "")) == "Cauciones"
        ]
        # Pases: par Venta(T) + Compra(T+1_biz_day, mismo simboloLocal)
        movs_pases = self._match_pases(all_movs, fecha_fmt)

        movs: list[dict] = []
        if "Cauciones" in tipos_config:
            movs.extend(movs_cauciones)
            logger.info("[%s] Cauciones el %s: %d", self.nombre, fecha_fmt, len(movs_cauciones))
        if "Pases" in tipos_config:
            movs.extend(movs_pases)
            logger.info("[%s] Pases el %s: %d (apertura+cierre)", self.nombre, fecha_fmt, len(movs_pases))

        if not movs:
            logger.info("[%s] Sin boletos para %s", self.nombre, fecha)
            return []

        # ── 4. Descargar PDFs ─────────────────────────────────────────────
        downloaded: list[Path] = []

        for m in movs:
            nro = m.get("numeroComprobante", "")
            concepto = m.get("concepto", "")
            tipo = self._classify_tipo(concepto)

            if tipo not in tipos_config:
                continue

            dest_tipo_dir = dest_dir / tipo
            dest_tipo_dir.mkdir(parents=True, exist_ok=True)

            url = f"{_API_BASE}/comprobantes/{quote(nro)}?formato=PDF"
            logger.info("[%s] Descargando %s → concepto=%r", self.nombre, nro, concepto)

            try:
                r = await page.context.request.get(url, headers=auth_h, timeout=timeout)
                body = await r.body()

                if body[:4] != b"%PDF":
                    logger.warning(
                        "[%s] %s no es PDF — status=%d  body=%r",
                        self.nombre, nro, r.status, body[:120],
                    )
                    continue

                fname = dest_tipo_dir / f"{nro.replace(' ', '_')}.pdf"
                fname.write_bytes(body)
                logger.info("[%s] Guardado: %s (%d bytes)", self.nombre, fname.name, len(body))
                downloaded.append(fname)

            except Exception as exc:
                logger.error(
                    "[%s] Error descargando %s — %s: %s",
                    self.nombre, nro, type(exc).__name__, exc,
                )

        logger.info("[%s] Total descargados: %d", self.nombre, len(downloaded))
        return downloaded

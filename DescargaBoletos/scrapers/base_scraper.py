import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

from playwright.async_api import async_playwright, Browser, Page


class BaseScraper(ABC):
    """
    Clase base para todos los scrapers de ALYCs.
    Gestiona el ciclo de vida del browser y la resolución de variables de entorno.
    Las subclases implementan login() y download_tickets() según el portal.
    """

    def __init__(self, alyc_config: dict, general_config: dict):
        self.nombre = alyc_config["nombre"]
        self.url_login = alyc_config["url_login"]
        self.usuario = self._resolve(alyc_config["usuario"])
        self.contrasena = self._resolve(alyc_config["contrasena"])
        self.opciones = alyc_config.get("opciones", {})
        # opciones["headless"] permite override por ALYC (ej. Max Capital requiere False por Cloudflare)
        if "headless" in self.opciones:
            self.headless = self.opciones["headless"]
        else:
            self.headless = general_config.get("headless", True)

        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def _resolve(self, value: str) -> str:
        """Expande referencias ${VAR} con el valor de la variable de entorno."""
        def replacer(match):
            var_name = match.group(1)
            resolved = os.environ.get(var_name)
            if resolved is None:
                raise EnvironmentError(
                    f"[{self.nombre}] Variable de entorno '{var_name}' no definida. "
                    "Verificar que el .env esté cargado."
                )
            return resolved

        return re.sub(r'\$\{(\w+)\}', replacer, value)

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await self._browser.new_context(accept_downloads=True)
        self._page = await context.new_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._page:
            await self._page.context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    @abstractmethod
    async def login(self) -> bool:
        """Realiza el login en el portal. Retorna True si fue exitoso."""
        ...

    @abstractmethod
    async def download_tickets(self, fecha: str, dest_dir: Path) -> list[Path]:
        """
        Descarga los boletos PDF de la fecha indicada (formato YYYY-MM-DD).
        Guarda los archivos en dest_dir y retorna la lista de paths descargados.
        """
        ...

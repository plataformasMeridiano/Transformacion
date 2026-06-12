"""
upload_cocos_cauciones_pamat.py

Sube los boletos de Cauciones de Cocos Capital (Pamat) desde una carpeta plana.
Extrae fecha_operacion y nro_boleto del texto de cada PDF.

Uso:
    python3 upload_cocos_cauciones_pamat.py "/mnt/c/Users/aduce/Downloads/cocos pamat"
"""
import json
import logging
import os
import re
import sys
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

from drive_uploader import DriveUploader

load_dotenv(Path(__file__).parent / ".env")

# Captura: comitente, fecha_operacion, fecha_liquidacion, nro_boleto
_RE_BOLETO = re.compile(
    r"(\d+)\s+"
    r"(\d{4}-\d{2}-\d{2})\s+"
    r"\d{4}-\d{2}-\d{2}\s+"
    r"(\d+)"
)


def extract_datos(pdf_path: Path) -> tuple[str, str] | None:
    """Retorna (fecha_operacion, nro_boleto) o None si no se puede extraer."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = _RE_BOLETO.search(text)
            if m:
                return m.group(2), m.group(3)  # fecha, nro
    return None


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/mnt/c/Users/aduce/Downloads/cocos pamat")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("cocos_cauciones_pamat")

    with open(Path(__file__).parent / "config.json") as f:
        cfg = json.load(f)
    gd = cfg["google_drive"]
    root_folder_id = re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    pdfs = sorted(folder.glob("*.pdf"))
    logger.info("PDFs encontrados: %d", len(pdfs))

    total = subidos = errores = sin_datos = 0

    for pdf_path in pdfs:
        total += 1
        datos = extract_datos(pdf_path)
        if not datos:
            logger.warning("Sin datos extraíbles: %s", pdf_path.name)
            sin_datos += 1
            errores += 1
            continue

        fecha, nro = datos
        logger.info("%-50s  fecha=%-10s  nro=%s", pdf_path.name, fecha, nro)

        try:
            uploader.upload_boleto(
                pdf_path=pdf_path,
                tipo_operacion="Cauciones",
                fecha=fecha,
                alyc_nombre="Cocos",
                nro_boleto=nro,
            )
            subidos += 1
        except Exception as exc:
            logger.error("Drive FALLÓ %s — %s", pdf_path.name, exc)
            errores += 1

    logger.info("=" * 60)
    logger.info("RESUMEN: total=%d  subidos=%d  sin_datos=%d  errores=%d",
                total, subidos, sin_datos, errores)


if __name__ == "__main__":
    main()

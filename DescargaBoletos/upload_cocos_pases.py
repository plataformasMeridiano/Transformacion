"""
upload_cocos_pases.py

Procesa el zip de boletos de pases de Cocos Capital:
  - Extrae el número de boleto de cada PDF (campo "Número" en el encabezado)
  - Sube a Drive como: Pases / YYYY-MM-DD / Boleto - Cocos - {nro}.pdf

Uso:
    python3 upload_cocos_pases.py "/mnt/c/Users/aduce/Downloads/BOLETOS PASES.zip"
"""
import io
import json
import logging
import os
import re
import sys
import zipfile
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

from drive_uploader import DriveUploader

load_dotenv(Path(__file__).parent / ".env")

# Regex para extraer el número de boleto:
# Busca la línea que contiene fecha de operación y al final el número
# Ej: "1555 2026-01-02 2026-01-05 43385"
_RE_NRO = re.compile(
    r"\d+\s+"              # comitente
    r"\d{4}-\d{2}-\d{2}\s+"  # fecha operación
    r"\d{4}-\d{2}-\d{2}\s+"  # fecha liquidación
    r"(\d+)"               # número de boleto ← captura
)


def extract_nro_boleto(pdf_bytes: bytes) -> str | None:
    """Extrae el número de boleto del texto del PDF."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = _RE_NRO.search(text)
            if m:
                return m.group(1)
    return None


def main():
    zip_path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/c/Users/aduce/Downloads/BOLETOS PASES.zip"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("cocos_pases")

    with open(Path(__file__).parent / "config.json") as f:
        cfg = json.load(f)
    gd = cfg["google_drive"]
    root_folder_id = re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], gd["root_folder_id"])
    uploader = DriveUploader(gd["credentials_file"], root_folder_id)

    total = subidos = errores = sin_nro = 0

    with zipfile.ZipFile(zip_path) as z:
        entries = [n for n in z.namelist() if n.endswith(".pdf")]
        logger.info("PDFs encontrados en zip: %d", len(entries))

        for entry in sorted(entries):
            total += 1
            parts = Path(entry).parts  # ('BOLETOS PASES', '20260102', 'DHSIO-Compra-102735134.pdf')
            if len(parts) < 3:
                logger.warning("Ruta inesperada: %s", entry)
                errores += 1
                continue

            raw_date = parts[-2]  # '20260102'
            try:
                fecha = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"  # '2026-01-02'
            except Exception:
                logger.warning("Fecha inválida en: %s", entry)
                errores += 1
                continue

            pdf_bytes = z.read(entry)

            nro = extract_nro_boleto(pdf_bytes)
            if not nro:
                logger.warning("Sin número de boleto: %s", entry)
                sin_nro += 1
                errores += 1
                continue

            logger.info("%-55s  fecha=%-10s  nro=%s", Path(entry).name, fecha, nro)

            try:
                # Guardar temporalmente en memoria no hace falta — uploader acepta Path
                # Escribimos a un temp file
                tmp = Path(f"/tmp/cocos_{nro}.pdf")
                tmp.write_bytes(pdf_bytes)

                uploader.upload_boleto(
                    pdf_path=tmp,
                    tipo_operacion="Pases",
                    fecha=fecha,
                    alyc_nombre="Cocos",
                    nro_boleto=nro,
                )
                tmp.unlink()
                subidos += 1
            except Exception as exc:
                logger.error("Drive FALLÓ %s — %s", entry, exc)
                errores += 1

    logger.info("=" * 60)
    logger.info("RESUMEN: total=%d  subidos=%d  sin_nro=%d  errores=%d",
                total, subidos, sin_nro, errores)


if __name__ == "__main__":
    main()

"""
upload_cocos_cauciones.py

Sube a Drive los boletos de Cauciones de Cocos Capital desde una carpeta local
organizada por fecha (YYYY-MM-DD/Boleto - Cocos - {nro}.pdf).

Uso:
    python3 upload_cocos_cauciones.py "/mnt/c/Users/aduce/OneDrive - Thetasoft/Documents/Claude/Projects/Meridiano - Transformación/boletos_renombrados"
"""
import json
import logging
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from drive_uploader import DriveUploader, nro_from_filename
from supabase_logger import log_boleto

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_TIPO = "Cauciones"
_ALYC = "Cocos"
_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Uso: python3 {Path(__file__).name} <carpeta_boletos>")
        return 1

    base_dir = Path(sys.argv[1])
    if not base_dir.is_dir():
        logger.error("Carpeta no encontrada: %s", base_dir)
        return 1

    with open(Path(__file__).parent / "config.json") as f:
        config = json.load(f)

    import os, re as _re
    def resolve(v):
        return _re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], v)

    gd = config["google_drive"]
    root_folder_id = resolve(gd["root_folder_id"])
    uploader = DriveUploader(
        gd["credentials_file"],
        root_folder_id,
        tipo_folder_overrides=gd.get("tipo_folder_overrides"),
    )

    fecha_dirs = sorted(
        [d for d in base_dir.iterdir() if d.is_dir() and _RE_FECHA.match(d.name)]
    )
    logger.info("Carpetas de fecha encontradas: %d", len(fecha_dirs))

    total_sub = total_err = 0

    for fecha_dir in fecha_dirs:
        fecha = fecha_dir.name
        pdfs = sorted(fecha_dir.glob("*.pdf"))
        if not pdfs:
            continue
        logger.info("%s — %d PDFs", fecha, len(pdfs))
        for pdf_path in pdfs:
            nro = nro_from_filename(pdf_path.name)
            try:
                file_id = uploader.upload_boleto(
                    pdf_path=pdf_path,
                    tipo_operacion=_TIPO,
                    fecha=fecha,
                    alyc_nombre=_ALYC,
                    nro_boleto=nro,
                )
                logger.info("  OK  %s  (id=%s)", pdf_path.name, file_id)
                log_boleto(fecha, _ALYC, _TIPO, nro, pdf_path.name, file_id)
                total_sub += 1
            except Exception as exc:
                logger.error("  ERR %s — %s", pdf_path.name, exc)
                total_err += 1

    logger.info("=" * 50)
    logger.info("TOTAL: subidos=%d  errores=%d", total_sub, total_err)
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

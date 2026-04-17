"""Sube a Drive los PDFs de Puente que ya están en disco pero no en Drive."""
import json, os, re, logging, sys
from pathlib import Path
from dotenv import load_dotenv
from drive_uploader import DriveUploader, nro_from_filename

load_dotenv(Path(".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])

with open("config.json") as f:
    cfg = json.load(f)
gd = cfg["google_drive"]
root_id = re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], gd["root_folder_id"])
uploader = DriveUploader(gd["credentials_file"], root_id)

FECHAS = ["2026-03-18"]

ok = err = 0
for fecha in FECHAS:
    base = Path(f"downloads/Puente/{fecha}")
    for tipo_dir in sorted(base.iterdir()):
        tipo = tipo_dir.name
        if tipo not in ("Cauciones", "Pases"):
            continue
        for pdf in sorted(tipo_dir.glob("*.pdf")):
            nro = nro_from_filename(pdf.name)
            if not nro or nro == "0":
                logging.warning("Saltando artefacto: %s", pdf.name)
                continue
            try:
                uploader.upload_boleto(pdf, tipo, fecha, "Puente", nro)
                logging.info("OK  %s/%s/%s", tipo, fecha, pdf.name)
                ok += 1
            except Exception as e:
                logging.error("FAIL %s/%s/%s — %s", tipo, fecha, pdf.name, e)
                err += 1

logging.info("RESUMEN: ok=%d  err=%d", ok, err)

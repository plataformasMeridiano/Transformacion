#!/usr/bin/env python3
"""Exporta la cartera de cheques y sus novedades a dos CSV en el Shared Drive.

Reemplaza a Jira como destino. Lo que antes iba a Jira eran dos cosas, y son los
dos archivos:

  cartera_cheques.csv    un renglon por cheque, con su estado actual (eran los issues)
  novedades_cheques.csv  el log de cambios de estado (eran las transiciones)

Los dos se REGENERAN ENTEROS desde Supabase en cada corrida. Es a proposito: el
CSV es una proyeccion de la base, no un log que se va apendeando, asi que no hay
forma de que quede un renglon duplicado o uno perdido si una corrida se corta a
la mitad — la siguiente lo vuelve a escribir completo.

Es un paso intermedio: el destino definitivo es Google Sheets.

Las novedades pendientes se marcan como resueltas (aplicado_por='csv') recien
despues de subir los archivos. Asi, si algun dia se prenden los webhooks por tipo,
no se disparan de golpe todas las novedades viejas.

Uso:
  exportar_cheques_csv.py            exporta, sube a Drive y marca
  exportar_cheques_csv.py --local    solo genera los CSV en disco (prueba)
"""

import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.request

ARG = dt.timezone(dt.timedelta(hours=-3))   # Argentina, sin horario de verano
SHARED_DRIVE = "0AEY4H4rWt18xUk9PVA"        # Shared Drive "Boletos"
CARPETA = "Cheques COELSA"
ESTADOS_NO_FINALES = ("ACTIVO", "DEPOSITADO", "PRESENTADO", "DEVOLUCION-PENDIENTE")
INICIO_FLUJO = "2026-08-11"

BASE = os.path.expanduser("~/Transformacion/DescargaBoletos")
ENV_FILE = os.environ.get("ENV_FILE", f"{BASE}/.env")
CRED_FILE = os.environ.get("GDRIVE_CREDENTIALS", f"{BASE}/credentials/gdrive_service_account.json")
SALIDA_LOCAL = os.environ.get("SALIDA_LOCAL", f"{BASE}/logs")


def cargar_env(ruta):
    env = {}
    with open(ruta, encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = cargar_env(ENV_FILE)


def supa(path, metodo="GET", cuerpo=None, prefer=None):
    headers = {"apikey": ENV["SUPABASE_KEY"], "Authorization": f"Bearer {ENV['SUPABASE_KEY']}",
               "Content-Type": "application/json", "Accept": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(
        f"{ENV['SUPABASE_URL']}/rest/v1/{path}",
        data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
        headers=headers, method=metodo)
    with urllib.request.urlopen(req, timeout=120) as r:
        texto = r.read().decode()
        return json.loads(texto) if texto else None


def todo(path):
    """Pagina de a 1000. Un select sin paginar se corta en el tope de PostgREST."""
    out, off = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        lote = supa(f"{path}{sep}limit=1000&offset={off}") or []
        out += lote
        if len(lote) < 1000:
            return out
        off += 1000


def hora_arg(ts):
    if not ts:
        return ""
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(ARG).strftime("%Y-%m-%d %H:%M")


def dia(ts):
    return str(ts or "")[:10]


def monto(x):
    # Punto decimal y sin separador de miles: es lo que Google Sheets lee como numero.
    return "" if x is None else f"{float(x):.2f}"


def es_cartera(c):
    # Misma regla que esCartera() en la edge function: lo que estuvo en Jira, lo que
    # sigue vivo, o lo que entro despues del arranque del flujo aunque ya este cobrado.
    return (bool(c.get("jira_issue_key"))
            or c.get("estado_norm") in ESTADOS_NO_FINALES
            or str(c.get("fecha_ingreso") or "") >= INICIO_FLUJO)


def a_csv(encabezado, filas):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(encabezado)
    w.writerows(filas)
    # BOM para que Excel abra bien los acentos; Google Sheets lo ignora.
    return "﻿" + buf.getvalue()


def armar():
    cheques = todo(
        "procesamiento_cheques?select=cheque_id,cheque_numero,monto,moneda,fecha_emision,"
        "fecha_pago,fecha_ingreso,librador,librador_cuit,banco_nombre,estado_norm,via_ingreso,"
        "contraparte_nombre,contraparte_cuit,jira_issue_key&order=fecha_ingreso.desc")
    por_id = {c["cheque_id"]: c for c in cheques}
    cartera = [c for c in cheques if es_cartera(c)]

    filas_cartera = [[
        hora_arg(c["fecha_ingreso"]), c["contraparte_nombre"] or "", c["contraparte_cuit"] or "",
        c["cheque_numero"] or "", monto(c["monto"]), c["moneda"] or "", dia(c["fecha_emision"]),
        dia(c["fecha_pago"]), c["librador"] or "", c["librador_cuit"] or "", c["banco_nombre"] or "",
        c["estado_norm"] or "", c["via_ingreso"] or "", c["jira_issue_key"] or "", c["cheque_id"],
    ] for c in cartera]

    novedades = todo("cheques_novedades?select=id,cheque_id,cheque_numero,jira_issue_key,tipo,"
                     "estado_anterior,estado_nuevo,detectado_at,enviado_at,detalle"
                     "&order=detectado_at.desc")
    filas_nov = []
    for n in novedades:
        d = n.get("detalle") or {}
        c = por_id.get(n["cheque_id"], {})
        filas_nov.append([
            hora_arg(n["detectado_at"]), n["tipo"], n["estado_anterior"] or "", n["estado_nuevo"] or "",
            n["cheque_numero"] or c.get("cheque_numero") or "",
            monto(d.get("monto", c.get("monto"))), dia(d.get("fecha_pago") or c.get("fecha_pago")),
            d.get("librador") or c.get("librador") or "",
            d.get("contraparte") or c.get("contraparte_nombre") or "",
            d.get("destino") or "", n["jira_issue_key"] or c.get("jira_issue_key") or "",
            n["cheque_id"], n["id"],
        ])

    archivos = {
        "cartera_cheques.csv": a_csv(
            ["Fecha de ingreso", "Cliente", "CUIT cliente", "Nº cheque", "Monto", "Moneda",
             "Fecha de emisión", "Fecha de pago", "Librador", "CUIT librador", "Banco",
             "Estado COELSA", "Vía de ingreso", "Issue Jira", "Cheque ID (COELSA)"],
            filas_cartera),
        "novedades_cheques.csv": a_csv(
            ["Detectada", "Tipo", "Estado anterior", "Estado nuevo", "Nº cheque", "Monto",
             "Fecha de pago", "Librador", "Cliente", "Destino", "Issue Jira",
             "Cheque ID (COELSA)", "ID novedad"],
            filas_nov),
    }
    pendientes = [n["id"] for n in novedades if not n.get("enviado_at")]
    return archivos, len(cartera), len(novedades), pendientes


def drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        CRED_FILE, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def buscar(svc, nombre, padre, mime=None):
    q = f"name='{nombre}' and '{padre}' in parents and trashed=false"
    if mime:
        q += f" and mimeType='{mime}'"
    r = svc.files().list(q=q, corpora="drive", driveId=SHARED_DRIVE, includeItemsFromAllDrives=True,
                         supportsAllDrives=True, fields="files(id,name)").execute()
    return (r.get("files") or [None])[0]


def subir(archivos):
    from googleapiclient.http import MediaIoBaseUpload
    svc = drive()
    carpeta = buscar(svc, CARPETA, SHARED_DRIVE, "application/vnd.google-apps.folder")
    if not carpeta:
        carpeta = svc.files().create(
            body={"name": CARPETA, "parents": [SHARED_DRIVE],
                  "mimeType": "application/vnd.google-apps.folder"},
            fields="id,name", supportsAllDrives=True).execute()
    links = {}
    for nombre, contenido in archivos.items():
        # Se sube como text/csv SIN conversion: tiene que seguir siendo un CSV.
        media = MediaIoBaseUpload(io.BytesIO(contenido.encode("utf-8")),
                                  mimetype="text/csv", resumable=False)
        ya = buscar(svc, nombre, carpeta["id"])
        if ya:
            # update conserva el file_id: el link compartido no cambia entre corridas.
            f = svc.files().update(fileId=ya["id"], media_body=media, fields="id,webViewLink",
                                   supportsAllDrives=True).execute()
        else:
            f = svc.files().create(body={"name": nombre, "parents": [carpeta["id"]]},
                                   media_body=media, fields="id,webViewLink",
                                   supportsAllDrives=True).execute()
        links[nombre] = f["webViewLink"]
    return links


def marcar(ids):
    ahora = dt.datetime.now(ARG).isoformat(timespec="seconds")
    for i in range(0, len(ids), 100):
        lote = ",".join(str(x) for x in ids[i:i + 100])
        supa(f"cheques_novedades?id=in.({lote})&enviado_at=is.null", "PATCH",
             {"enviado_at": ahora, "aplicado_por": "csv"}, prefer="return=minimal")


def main():
    solo_local = "--local" in sys.argv
    archivos, n_cartera, n_nov, pendientes = armar()

    # Copia local siempre: si Drive falla, el dato igual queda en la VM.
    os.makedirs(SALIDA_LOCAL, exist_ok=True)
    for nombre, contenido in archivos.items():
        with open(os.path.join(SALIDA_LOCAL, nombre), "w", encoding="utf-8", newline="") as fh:
            fh.write(contenido)

    if solo_local:
        print(f"LOCAL cartera={n_cartera} novedades={n_nov} pendientes={len(pendientes)} -> {SALIDA_LOCAL}")
        return 0

    try:
        subir(archivos)
    except Exception as e:  # noqa: BLE001
        # No se marca nada: las pendientes quedan para la corrida siguiente.
        print(f"CSV FALLA al subir a Drive: {str(e)[:200]} | cartera={n_cartera} novedades={n_nov}")
        return 1

    marcar(pendientes)
    print(f"CSV cartera={n_cartera} novedades={n_nov} marcadas={len(pendientes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

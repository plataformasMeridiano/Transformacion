#!/usr/bin/env python3
"""Puente TEMPORAL: mueve los issues de Jira segun las novedades pendientes.

El flujo definitivo es webhook por tipo: sync-cmf-cheques detecta la novedad y
dispara, y una automation de Jira hace la transicion. Mientras esos webhooks no
existan, las novedades se apilan sin que el issue se entere. Este script las
drena usando las transiciones provisorias que hay hoy en el workflow.

Es un ATAJO y esta pensado para borrarse: cuando los webhooks esten activos, se
saca la linea que lo invoca en sync_cheques_novedades.sh y listo. La columna
`aplicado_por` deja registrado que paso por aca.

Solo mueve tres tipos. Los demas (CADUCADO, DEVOLUCION_PENDIENTE, REPUDIADO,
SALIO_DE_CARTERA) quedan pendientes a proposito: para esos no hay transicion que
sea correcta sin mirarlos a mano.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Estado de COELSA -> nombre del estado en el workflow de Jira.
DESTINO = {
    "DEPOSITADO": "Depositado",
    "ACREDITADO": "Acreditado",
    "RECHAZADO": "Rechazado",
}

JIRA_BASE = "https://meridianonorte.atlassian.net"
TOPE = int(os.environ.get("TOPE_TRANSICIONES", "200"))


def cargar_env(ruta):
    env = {}
    with open(ruta, encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = cargar_env(os.environ.get(
    "ENV_FILE", os.path.expanduser("~/Transformacion/DescargaBoletos/.env")))

AUTH = "Basic " + base64.b64encode(
    f"{ENV['JIRA_EMAIL']}:{ENV['JIRA_API_TOKEN']}".encode()).decode()


def jira(path, metodo="GET", cuerpo=None):
    req = urllib.request.Request(
        f"{JIRA_BASE}/rest/api/3/{path}",
        data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
        headers={"Authorization": AUTH, "Accept": "application/json",
                 "Content-Type": "application/json"},
        method=metodo)
    with urllib.request.urlopen(req, timeout=90) as r:
        cuerpo = r.read().decode()
        return r.status, (json.loads(cuerpo) if cuerpo else None)


def supa(path, metodo="GET", cuerpo=None, prefer=None):
    headers = {"apikey": ENV["SUPABASE_KEY"],
               "Authorization": f"Bearer {ENV['SUPABASE_KEY']}",
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


def jira_reintentando(path, intentos=4):
    """GET a Jira aguantando errores transitorios. Devuelve None si nunca contesto.

    Jira devuelve 5xx y 429 cada tanto — el 2026-09-08 fue un 522 de Cloudflare — y
    sin esto la excepcion se propagaba y mataba la corrida entera.
    """
    for i in range(1, intentos + 1):
        try:
            _, d = jira(path)
            return d
        except urllib.error.HTTPError as e:
            # 4xx que no sea 429 es un error nuestro: reintentar no lo arregla.
            if e.code != 429 and 400 <= e.code < 500:
                raise
            ultimo = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            ultimo = str(e)[:120]
        if i < intentos:
            time.sleep(3 * i)
    print(f"  Jira no contesto tras {intentos} intentos ({ultimo}): {path[:80]}")
    return None


def estados_actuales(keys):
    """Estado actual de muchos issues, con JQL. Uno por uno son 400+ requests.

    Devuelve (estados, sin_respuesta). Los dos valores son necesarios: si un lote
    falla y se devolviera solo el mapa parcial, esos issues quedarian sin entrada,
    o sea **indistinguibles de "el issue no existe"**, y el llamador los marcaria
    con un error falso. Es la misma trampa de la ausencia que hizo sacar la
    repesca: cuando un dato se define por lo que NO esta, hay que poder demostrar
    que se miro de verdad.
    """
    out = {}
    sin_respuesta = set()
    for i in range(0, len(keys), 90):
        lote = keys[i:i + 90]
        jql = f"key in ({','.join(lote)})"
        token = None
        while True:
            url = f"search/jql?jql={urllib.parse.quote(jql)}&fields=status&maxResults=100"
            if token:
                url += f"&nextPageToken={token}"
            d = jira_reintentando(url)
            if d is None:
                # Se pierde el lote entero, no solo la pagina: sin saber que trajo
                # la pagina anterior no se puede afirmar nada sobre ninguno.
                sin_respuesta.update(lote)
                for k in lote:
                    out.pop(k, None)
                break
            for it in d.get("issues") or []:
                out[it["key"]] = it["fields"]["status"]["name"]
            token = d.get("nextPageToken")
            if not token:
                break
    return out, sin_respuesta


def marcar(novedad_id, aplicado_por, http_status):
    supa(f"cheques_novedades?id=eq.{novedad_id}", "PATCH", {
        "enviado_at": time.strftime("%Y-%m-%dT%H:%M:%S-03:00"),
        "aplicado_por": aplicado_por,
        "http_status": http_status,
        "error_msg": None,
    }, prefer="return=minimal")


def fallo(novedad, mensaje):
    supa(f"cheques_novedades?id=eq.{novedad['id']}", "PATCH", {
        "intentos": (novedad.get("intentos") or 0) + 1,
        "error_msg": str(mensaje)[:400],
    }, prefer="return=minimal")


def main():
    pendientes = supa(
        "cheques_novedades?select=id,tipo,jira_issue_key,estado_anterior,estado_nuevo,intentos"
        "&enviado_at=is.null&jira_issue_key=not.is.null"
        f"&tipo=in.({','.join(DESTINO)})"
        f"&order=detectado_at.asc&limit={TOPE}") or []

    if not pendientes:
        print("sin novedades pendientes que muevan issues")
        return 0

    estado, sin_respuesta = estados_actuales(
        sorted({n["jira_issue_key"] for n in pendientes}))

    movidos = ya = errores = salteados = 0
    for n in pendientes:
        key = n["jira_issue_key"]
        destino = DESTINO[n["tipo"]]
        actual = estado.get(key)

        # Jira no contesto por este issue. NO es lo mismo que "no existe": la
        # novedad se deja intacta (sin sumar intentos ni escribir un error que
        # despues confunda) y la corrida siguiente la vuelve a mirar.
        if key in sin_respuesta:
            salteados += 1
            continue

        if actual is None:
            fallo(n, "el issue no existe o no es visible")
            errores += 1
            continue

        # Idempotencia: el bulk manual ya dejo muchos issues en su estado. Volver a
        # transicionar no solo es ruido, en varios estados directamente no hay
        # transicion de salida y seria un error inventado.
        if actual == destino:
            marcar(n["id"], "ya-en-estado", None)
            ya += 1
            continue

        try:
            _, d = jira(f"issue/{key}/transitions")
            opciones = d.get("transitions") or []
            elegida = next((t for t in opciones if t["to"]["name"] == destino), None)
            if not elegida:
                fallo(n, f"desde '{actual}' no hay transicion a '{destino}'; "
                         f"disponibles: {[t['to']['name'] for t in opciones]}")
                errores += 1
                continue
            st, _ = jira(f"issue/{key}/transitions", "POST",
                         {"transition": {"id": elegida["id"]}})
            marcar(n["id"], "transicion-directa", st)
            movidos += 1
            print(f"  {key}: {actual} -> {destino}  (novedad {n['tipo']})")
        except urllib.error.HTTPError as e:
            fallo(n, f"HTTP {e.code}: {e.read().decode()[:200]}")
            errores += 1
        except Exception as e:  # noqa: BLE001
            fallo(n, e)
            errores += 1

    # salteados va en el resumen aunque sea 0: es la unica senal de que Jira se
    # cayo y quedo trabajo sin mirar. Si no se imprime, una corrida que no pudo
    # hacer nada se lee igual que una donde no habia nada que hacer.
    print(f"movidos={movidos} ya_en_estado={ya} errores={errores} "
          f"salteados={salteados} de {len(pendientes)} pendientes")
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())

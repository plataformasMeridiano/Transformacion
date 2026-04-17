"""
diag_metro_boleto.py — Explora el endpoint correcto de descarga de boleto en MetroCorp.

1. Login → captura bearer token
2. Llama metrocorp.list para fecha conocida (2026-02-10 = boleto 69099 CAUCION)
3. Muestra estructura del movimiento
4. Prueba endpoints candidatos para descarga individual
5. Si encuentra PDF, lo guarda

Uso:
    python diag_metro_boleto.py
"""
import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(".env"))
import os

DNI      = os.environ.get("METRO_DOCUMENTO", "13654870")
USUARIO  = os.environ.get("METRO_USUARIO",   "Meri1496")
PASSWORD = os.environ.get("METRO_PASSWORD",  "")

# Fecha con cauciones conocidas (boleto 69099, cuenta 33460)
FECHA_TEST    = "2026-02-10"
CUENTA        = "33460"
ID_ENVIRONMENT = 407

OUT_DIR = Path("downloads/diag_metro_boleto")
OUT_DIR.mkdir(parents=True, exist_ok=True)

URL_LOGIN     = "https://be.bancocmf.com.ar/"
URL_METROCORP = "https://be.bancocmf.com.ar/metrocorp"
API_BASE      = "https://be.bancocmf.com.ar/api/v1/execute"

from playwright.async_api import async_playwright


def make_iso(fecha_str: str) -> str:
    dt = datetime.strptime(fecha_str, "%Y-%m-%d")
    return dt.replace(hour=3, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def api_post(page, endpoint: str, body: dict, bearer: str) -> dict:
    """POST a /api/v1/execute/{endpoint} via browser fetch."""
    result = await page.evaluate(
        """async ([url, bodyStr, auth]) => {
            try {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json;charset=UTF-8',
                        'Accept': 'application/json, application/octet-stream',
                        'Authorization': auth
                    },
                    credentials: 'include',
                    body: bodyStr
                });
                const bodyAb = await r.arrayBuffer();
                const bytes = new Uint8Array(bodyAb);
                let b64 = '';
                const CHUNK = 8192;
                for (let i = 0; i < bytes.byteLength; i += CHUNK)
                    b64 += String.fromCharCode(...bytes.subarray(i, Math.min(i+CHUNK, bytes.byteLength)));
                return { ok: r.ok, status: r.status,
                         ct: r.headers.get('content-type') || '',
                         b64: btoa(b64), len: bytes.byteLength };
            } catch(e) {
                return { ok: false, error: e.toString() };
            }
        }""",
        [f"{API_BASE}/{endpoint}", json.dumps(body), bearer],
    )
    if result.get("error"):
        print(f"  ERROR en {endpoint}: {result['error']}")
        return {}
    raw = base64.b64decode(result["b64"])
    if b"%PDF" in raw[:10]:
        return {"_pdf_binary": raw, "_status": result["status"]}
    if "json" in result.get("ct", ""):
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw[:500].decode("utf-8", errors="replace")}
    return {"_raw": raw[:500].decode("utf-8", errors="replace"), "_status": result["status"]}


async def main():
    bearer = ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        page = await context.new_page()

        # Capturar bearer
        async def on_response(resp):
            nonlocal bearer
            if "/oauth/token" in resp.url:
                try:
                    j = json.loads(await resp.body())
                    if "access_token" in j:
                        bearer = f"bearer {j['access_token']}"
                        print("  *** Bearer capturado")
                except Exception:
                    pass
        page.on("response", on_response)

        # ── Login ──────────────────────────────────────────────────────────────
        print(f"\n[1] Login")
        await page.goto(URL_LOGIN, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3000)
        await page.wait_for_selector("#document\\.number", timeout=30_000)
        await page.fill("#document\\.number", DNI)
        await page.fill("#login\\.step1\\.username", USUARIO)
        await page.click("button[type='submit']:has-text('Continuar')")
        await page.wait_for_selector("#login\\.step2\\.password", timeout=30_000)
        await page.fill("#login\\.step2\\.password", PASSWORD)
        await page.click("button[type='submit']:has-text('Ingresar')")
        await page.wait_for_url(lambda u: "desktop" in u, timeout=30_000)
        await page.wait_for_timeout(2000)
        print(f"  Login OK  —  bearer: {'OK' if bearer else 'MISSING'}")

        # ── Activar contexto metrocorp ────────────────────────────────────────
        print(f"\n[2] Activando contexto metrocorp")
        await page.goto(URL_METROCORP, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)

        # ── Llamar metrocorp.list para fecha conocida ─────────────────────────
        iso = make_iso(FECHA_TEST)
        print(f"\n[3] metrocorp.list  fecha={FECHA_TEST}  iso={iso}")
        list_resp = await api_post(page, "metrocorp.list", {
            "optionSelected": "movements",
            "principalAccount": CUENTA,
            "species": "all",
            "date":     iso,
            "dateFrom": iso,
            "dateTo":   iso,
            "page": 1,
            "idEnvironment": ID_ENVIRONMENT,
            "lang": "es",
            "channel": "frontend",
        }, bearer)

        code = list_resp.get("code")
        data = list_resp.get("data", {})
        movements = data.get("movements", [])
        print(f"  code={code}  movements={len(movements)}")

        if not movements:
            print("  Sin movimientos — probando otras fechas...")
            for f in ["2026-02-09", "2026-02-11", "2026-02-12", "2026-02-13"]:
                iso2 = make_iso(f)
                r2 = await api_post(page, "metrocorp.list", {
                    "optionSelected": "movements",
                    "principalAccount": CUENTA,
                    "species": "all",
                    "date": iso2, "dateFrom": iso2, "dateTo": iso2,
                    "page": 1,
                    "idEnvironment": ID_ENVIRONMENT,
                    "lang": "es", "channel": "frontend",
                }, bearer)
                movs2 = r2.get("data", {}).get("movements", [])
                print(f"  {f}: {len(movs2)} movimientos")
                if movs2:
                    movements = movs2
                    data = r2.get("data", {})
                    print(f"  Usando fecha {f}")
                    break

        if not movements:
            print("  ERROR: sin movimientos en ninguna fecha de prueba")
            await context.close()
            await browser.close()
            return

        # ── Mostrar estructura de los movimientos ─────────────────────────────
        print(f"\n[4] Estructura de movimientos:")
        for i, m in enumerate(movements):
            print(f"\n  Movimiento [{i}]:")
            for k, v in m.items():
                print(f"    {k}: {v!r}")

        # Guardar el movimiento completo para análisis
        (OUT_DIR / "movements.json").write_text(
            json.dumps(movements, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n  Movimientos guardados en movements.json")

        # ── Probar endpoints candidatos para descarga individual ──────────────
        print(f"\n[5] Probando endpoints de descarga individual")

        # Tomar el primer movimiento de Cauciones
        cauc_mov = None
        for m in movements:
            desc = (m.get("descripcionOperacion") or "").upper()
            if "CAUC" in desc or "CIERRE" in desc or "APER" in desc:
                cauc_mov = m
                break
        if not cauc_mov:
            cauc_mov = movements[0]  # usar el primero si no hay cauciones

        print(f"  Movimiento a usar: nro={cauc_mov.get('numeroBoleto')} desc={cauc_mov.get('descripcionOperacion')!r}")

        # Campos que podría usar el endpoint de descarga individual
        nro_boleto = cauc_mov.get("numeroBoleto", "")
        id_mov = cauc_mov.get("id") or cauc_mov.get("idMovimiento") or cauc_mov.get("idTransaction") or ""
        print(f"  numeroBoleto={nro_boleto!r}  id={id_mov!r}")
        print(f"  Todos los campos: {list(cauc_mov.keys())}")

        # Candidatos a probar
        candidates = [
            # Por número de boleto
            ("metrocorp.downloadDetail",     {"numeroBoleto": nro_boleto, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            ("metrocorp.downloadVoucher",    {"numeroBoleto": nro_boleto, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            ("metrocorp.voucher",            {"numeroBoleto": nro_boleto, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            ("metrocorp.boleto",             {"numeroBoleto": nro_boleto, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            ("metrocorp.downloadBoleto",     {"numeroBoleto": nro_boleto, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            ("metrocorp.comprobante",        {"numeroBoleto": nro_boleto, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            ("metrocorp.downloadComprobante",{"numeroBoleto": nro_boleto, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            # Con el movimiento completo
            ("metrocorp.downloadDetail",     {"movement": cauc_mov, "idEnvironment": ID_ENVIRONMENT, "format": "pdf", "lang": "es", "channel": "frontend"}),
            ("metrocorp.downloadDetail",     {"movements": [cauc_mov], "idEnvironment": ID_ENVIRONMENT, "format": "pdf", "lang": "es", "channel": "frontend"}),
            # Con summary (similar a downloadList pero solo uno)
            ("metrocorp.downloadDetail",     {
                "summary": {
                    "movements": [cauc_mov],
                    "optionSelected": "movements",
                    "filtersData": {
                        "principalAccount": CUENTA,
                        "dateFrom": iso,
                        "dateTo": iso,
                    }
                },
                "format": "pdf",
                "idEnvironment": ID_ENVIRONMENT,
                "lang": "es", "channel": "frontend"
            }),
            ("metrocorp.detail",             {"movement": cauc_mov, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
            ("metrocorp.getDetail",          {"movement": cauc_mov, "idEnvironment": ID_ENVIRONMENT, "lang": "es", "channel": "frontend"}),
        ]

        for endpoint, body in candidates:
            print(f"\n  Probando {endpoint}  body_keys={list(body.keys())}")
            try:
                resp = await api_post(page, endpoint, body, bearer)
                if "_pdf_binary" in resp:
                    fname = OUT_DIR / f"boleto_{endpoint.replace('.', '_')}.pdf"
                    fname.write_bytes(resp["_pdf_binary"])
                    print(f"  *** PDF ENCONTRADO ({len(resp['_pdf_binary'])} bytes) → {fname.name}")
                elif "_raw" in resp:
                    print(f"  raw ({resp.get('_status')}): {resp['_raw'][:150]!r}")
                else:
                    code2 = resp.get("code", "?")
                    data2 = resp.get("data", {})
                    msg2  = resp.get("message", "")
                    print(f"  JSON: code={code2}  data_keys={list(data2.keys()) if isinstance(data2, dict) else type(data2).__name__}  msg={msg2!r}")
                    # Guardar respuesta completa para análisis
                    if code2 != "ERR":
                        fname = OUT_DIR / f"resp_{endpoint.replace('.', '_')}.json"
                        fname.write_text(json.dumps(resp, indent=2, ensure_ascii=False))
            except Exception as e:
                print(f"  EXCEPCION: {e}")

        # ── Pausa para interacción manual si nada funcionó ────────────────────
        print(f"""
========================================================
  EXPLORACION COMPLETADA

  Si ningún endpoint dio PDF arriba, interactuá manualmente
  en el browser (60 segundos):
  1. Hacé click en el ícono del OJO de una fila Caucion
  2. En el popup, hacé click en DESCARGA

  Monitoreá la pestaña Network del DevTools del browser
  para ver qué endpoint se llama.
========================================================
        """)
        await page.wait_for_timeout(60_000)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

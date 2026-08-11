# DescargaBoletos — Contexto del Proyecto

## Objetivo
Descargar comprobantes PDF (boletos) de **cauciones y pases** de múltiples ALYCs de forma automática, y subirlos a Google Drive organizados por tipo de operación, fecha y ALYC.

## Arquitectura general

- `main.py` — orquestador diario; procesa ayer por defecto o una fecha específica por CLI
- `batch_download.py` — utilidades para backfills y retries de rangos de fechas
- `drive_uploader.py` — sube PDFs a Google Drive vía service account
- `scrapers/` — un scraper por sistema de portal web
- `config.json` — configuración de ALYCs (credenciales via `${ENV_VAR}`)
- `.env` — variables de entorno con credenciales reales (no commitear)
- `credentials/gdrive_service_account.json` — credenciales de Drive

## ALYCs y sistemas

| ALYC | Sistema | Archivo | Notas |
|------|---------|---------|-------|
| Puente | sistemaA | `alyc_sistemaA.py` | Playwright + persistent context Chrome; inputs de fecha controlados por AngularJS — se resolvió el problema de fill |
| ADCAP | sistemaB | `alyc_sistemaB.py` | Comparte sistema con Criteria, BACS y DA Valores |
| Criteria | sistemaB | `alyc_sistemaB.py` | |
| BACS | sistemaB | `alyc_sistemaB.py` | Solo Pases |
| DA Valores | sistemaB | `alyc_sistemaB.py` | Solo MeridianoNorte; URL: `https://clientes.davalores.com.ar/VBHome/login.html#!/login`; creds: `${DA_VALORES_USUARIO}` / `${DA_VALORES_PASSWORD}` |
| WIN | sistemaG | `alyc_sistemaG.py` | **Migró a plataforma Fermi (Auth0)** el 2026-07 — comparte scraper con Dhalmore. Cuentas: MeridianoNorte (64346/50015), Mancia (64347/50016), Pamat (64348/50017). Login por email. `alyc_sistemaC.py` (portal ASP.NET viejo) quedó obsoleto. |
| ConoSur | sistemaD | `alyc_sistemaD.py` | Dos instancias: cuenta 3003 (MN) y 3087 (Pamat) |
| MaxCapital | sistemaE | `alyc_sistemaE.py` | headless=false; cuentas MN (20759) y Pamat (20774) |
| MetroCorp | sistemaF | `alyc_sistemaF.py` | Solo Cauciones |
| Dhalmore | sistemaG | `alyc_sistemaG.py` | headless=false; cuentas MN (56553) y Pamat (56555). sistemaG es config-driven (api_base/url_base/profile_dir/device_id por opciones) — reusado por WIN |
| Allaria | sistemaH | `alyc_sistemaH.py` | **Migró a plataforma propia (`app.allaria.com.ar` + API `api.allaria.cloud`) en mayo-2026.** Auth0/SSO + TOTP; persistent context; API-based (ya no hereda AdcapScraper); colocadoras + FCE. Comitente 131864. Ver la limitación de fecha de concertación abajo |
| IEB | ieb | `alyc_ieb.py` | Portal propio ASP.NET MVC; comitente 365533; solo MeridianoNorte; cauciones por **concertación** vía OperacionesDia (proceso=05) + CLAV de proceso=02 |

## Cuentas comitentes

Dos entidades principales presentes en varias ALYCs:
- **MeridianoNorte** — cuenta operativa principal
- **Pamat** — cuenta operativa secundaria

## Estructura de archivos descargados

```
downloads/
└── {ALYC}/
    └── {YYYY-MM-DD}/
        ├── Cauciones/
        │   └── {id}.pdf
        └── Pases/
            └── {id}.pdf
```

## Estado del proyecto (al 2026-07-06)

- Todos los scrapers implementados y funcionando en producción

- **Puente (sistemaA):** se resolvió el problema de seteo de fechas — los inputs `#fechaDesde`/`#fechaHasta` están controlados por AngularJS y `page.fill()` no actualizaba el modelo; se implementó la solución correcta.

- **Puente — nombres de archivo corregidos:** los boletos se guardaban con el `idMovimiento` de la URL (ej: `16437291.pdf`) en lugar del número de boleto real. Se corrigió leyendo el header `Content-Disposition` del response de descarga (ej: `filename="13841 - Movimiento 9304.pdf"` → se guarda como `9304.pdf`). Se re-descargaron y re-subieron las 38 fechas afectadas (15-ene a 12-mar-2026) con `run_puente_fix_nombres.py`, y se eliminaron los ~158 archivos viejos de Drive con `cleanup_puente_nombres_drive.py`.

- **DA Valores (sistemaB):** agregado 2026-03-20. Mismo portal VBhome/Unisync que ADCAP. Solo cuenta MeridianoNorte. Backfill completo: 66 boletos en 19 fechas (2026-02-23 → 2026-03-19). Zapier procesado para todas esas fechas con `run_da_zapier.py` (19/19 OK, `status=Fin Cauciones`).

- **DA Valores — nombre de archivo en Drive (2026-07-17, commit `6e370ca`):** los boletos se guardaban como `Boleto - DAValores - {nro}.pdf` y debían ser `Boleto - DA Valores - {nro}.pdf`. Se agregó `_ALYC_DRIVE_NAME` en `drive_uploader.py` (ver Notas técnicas): el identificador interno sigue siendo `DAValores`, solo cambia el nombre visible del archivo. Aplica a todos los tipos (Pases, Cauciones, FCE, Colocadoras). **Los 743 boletos históricos ya en Drive se renombraron** in-place (mismo `file_id`, no rompe `drive_file_id` de Supabase ni links de Jira): Pases 532, Cauciones 146, Venta FCE-eCheq 63, Colocadoras 2 (feb→jul), 0 errores. Con el rename no quedan duplicados al reprocesar fechas viejas.

- **IEB (scraper propio):** agregado 2026-07-06. Portal ASP.NET MVC en `clientesv2.invertirenbolsa.com.ar`. Solo comitente MeridianoNorte (365533). Descarga via `GetComprobante {clave: CLAV}` → base64 PDF.

- **IEB — descarga por fecha de CONCERTACIÓN (rediseño 2026-07-14, commit `2d32cfb`):** el enfoque original archivaba las cauciones filtrando `CuentaCorrientePesos` (proceso=02) por `FEC1`, que para la pata de cierre (`TOCT`) es la **fecha de liquidación** → el cierre quedaba bajo su vencimiento y no bajo la concertación, y se perdían cierres de plazo largo. Nuevo flujo en dos pasos:
  1. **Identificar** con **OperacionesDia** = `GetConsulta proceso=05`, `fechaDesde=<día>` (por concertación). Devuelve `Result.Operaciones[]` agrupado por especie; cada uno con `Detalle[]` de patas `{CPTE, ESPE, Comprobante, NUME, CLAVE, ...}`. Lista **apertura + cierre(s) de cada caución juntos el día de la concertación** (el portal publica el boleto del cierre con fecha futura), y también títulos y FCE-eCheq.
  2. **Descargar** con la `CLAV` de **proceso=02** (la `CLAVE` de proceso=05 **no** sirve para `GetComprobante` — devuelve HTML). Se arma un mapa `NUME→CLAV` con proceso=02 y **ventana forward de 120 días** (`_CLAV_WINDOW_DAYS`) para alcanzar la fecha de liquidación de cauciones a plazo largo; el ledger ya trae el boleto futuro con su CLAV.
  Todo se archiva bajo la **fecha de concertación** sin tocar `batch_download.py`. Clasificación por CPTE: `TCC`/`TOCT`=Cauciones, `VCMV`+Nombre "FACTURA ELECTRONICA"=Venta FCE-eCheq, `VRCN`/`CRCN`/`VTIN`=Títulos (vía `titulos_codes`, configurable). `RFCW`/`SFCI`=FCI (se ignoran). `caucion_codes` default `["TCC","TOCT"]`; `colocadoras_codes`/`pase_codes`/`titulos_codes` vacíos por defecto.
  - **Backfill junio 2026 rehecho:** se mandaron a papelera los 15 boletos viejos mal fechados y se re-descargaron **20 boletos** correctos por concertación (17–30/jun). Incluye cierres de plazo largo antes perdidos (ej. caución 23/06 con cierre `807620` que liquida 23/07). `pase_codes`/`colocadoras_codes` aún por descubrir; Títulos IEB soportado pero **inactivo** (falta agregar `"Títulos"` a `tipo_operacion` + `titulos_codes`).

- **Allaria — migración a plataforma propia (2026-08-11):** Allaria abandonó VBolsaNet y migró a `app.allaria.com.ar` con API REST en `api.allaria.cloud`. El scraper venía fallando **desde el 18-may** con un falso "Login Auth0 no completó" — el login andaba bien, lo que fallaba era el chequeo de URL de destino. `sistemaH` se reescribió API-based y dejó de heredar `AdcapScraper`.
  - **Listado:** `GET /by-mas/movements?account-id=131864&from-date=&to-date=&company=ALLARIA&criteria=SETTLEMENT&currency=ARS`
  - **PDF:** `GET /api/tickets/{operation_id}/accounts/{cuenta}/movements/{id}` (500 = movimiento sin boleto, saltear)
  - **Header obligatorio `x-client-origin: ALLARIA`** — sin él, 401. Y hay que salir del navegador (un `fetch()` desde la página falla por preflight CORS): se usa el cliente HTTP de Playwright.
  - **Capturar el bearer del host exacto `https://api.allaria.cloud/`** y quedarse con el **más reciente**: `login.api.allaria.cloud` y `market-data.api.allaria.cloud` contienen ese substring pero usan otra audiencia, y antes de autenticarse el SPA ya emite un token anónimo. Ambos casos dan 401.
  - **Clasificar por `market_operation_type`**, NO por `metadata.operationTypeId`: este último falta en el 84% de los registros (de 228 ventas de cheque, 143 no lo traen). `VENTA_CHEQ`→Venta FCE-eCheq; `APERTURA_COLOCADOR_CONTADO`/`_FUTURO`→Cauciones Colocadoras. Descartar `state=REJECTED`.
  - **Nº de boleto:** no está en el JSON (`ticket_id` siempre null; `metadata.id` es otro id). Se extrae del texto del PDF con pdfplumber: `BOLETO #676265`. Verificado que coincide con la numeración histórica.

- **⚠️ Allaria — la fecha de concertación es una limitación abierta (solución temporal):** la API **no permite consultar por concertación**. `from-date`/`to-date` filtran por **liquidación** (probado: una colocadora concertada el 16/03 que liquida el 17/03 no aparece en la ventana del 16 y sí en la del 17); `criteria=AGREEMENT` es un valor válido pero devuelve 0 siempre; `/broker/operations` (solapa Órdenes) está vacío porque Meridiano opera por mesa; `/api/tickets` no expone listado; y la UI ni siquiera deja elegir fechas futuras. El motivo de fondo es que **la lista es un ledger de movimientos ya liquidados** (603 de 609 en `LIQUIDATED`, ninguno con liquidación futura): una operación no existe en la API hasta que liquida.
  **Mientras tanto** se pide una ventana de liquidación hacia adelante (`ventana_dias`, default 45) y se archiva por `agreement_at`. Para backfills es exacto; en el día a día un boleto que liquida más tarde aparece recién entonces y lo levanta el `--delta`. **No es la solución definitiva**: hay que conseguir de Allaria un listado por concertación. Cuando exista, `download_tickets` se reduce a una consulta de un día y se borra toda la lógica de ventana.

- **Títulos — soporte multi-scraper:** agregado 2026-07-06. Todos los scrapers (sistemaA–G) soportan tipo "Títulos". Configuración por ALYC: `titulos_codes` (sistemaB/E), `titulos_conceptos` (sistemaD), `titulos_keywords` (sistemaF). En sistemaA, "Venta" de Títulos excluye filas con "%" (esas son FCE-eCheq). En sistemaG (Dhalmore), tipo API pendiente de identificar. En sistemaE (MaxCapital), se corrigió extracción de nro boleto para formato MAE (`Boleto MAE #XXXXX`).

- **WIN — migración a plataforma Fermi (2026-07-14, commit `623ee17`):** WIN abandonó su portal ASP.NET (`clientes.winsa.com.ar`, sistemaC) y migró a la plataforma Fermi/Auth0 (`login.winsa.com.ar`, API `core.winsa.prod.fermi.galloestudio.com`) — la misma que Dhalmore. El scraper viejo dejó de autenticar el 04-jun (login rebotaba en silencio, `login()` reportaba éxito falso) → 0 cauciones ~6 semanas. Se **generalizó sistemaG** (config-driven vía `api_base`/`url_base`/`profile_dir`/`device_id`) y WIN pasó a `sistemaG` con 3 cuentas (MN 64346, Mancia 64347, Pamat 64348), login por email, `tipo_operacion=Cauciones`. Vault actualizado (`WIN-USUARIO`=djoy@…, `WIN-PASSWORD`) vía `update-secret`. Backfill 04-jun→14-jul: **104 boletos** subidos a Drive (`run_win_backfill.py`, one-off). **Pendiente:** primer login en la VM pide device-verification MFA (código a `/tmp/win_code.txt`) para dejar el perfil persistente; y sync Zapier de esas fechas para reflejar en Jira.

- **Cocos Capital — carga manual de pases:** Cocos no tiene scraper; los boletos se reciben como zip con estructura `BOLETOS PASES/YYYYMMDD/INSTRUMENTO-TipoOp-ID.pdf`. El número de boleto real se extrae del texto del PDF (campo "Número" en el encabezado: línea con comitente + fecha operación + fecha liquidación + número). Script: `upload_cocos_pases.py`. Carga inicial: 230 PDFs desde 2026-01-02, subidos a `Pases / YYYY-MM-DD / Boleto - Cocos - {nro}.pdf`.

- **Nota Drive:** el service account tiene `canTrash=True` pero `canDelete=False` en el Shared Drive — los borrados se hacen con `files().update(trashed=True)`, no con `files().delete()`.

## Flujo Zapier / Supabase

El procesamiento de boletos ocurre vía webhook de Zapier:
- **Webhook:** `https://hooks.zapier.com/hooks/catch/24963922/uqqfupo/`
- **Parámetros:** `fecha` (requerido), `alyc` (opcional — si se omite, procesa todas)
- **Tabla Supabase:** `Procesamiento_Cauciones` con campos `fecha_operacion`, `alyc`, `status`
- **Condición de completado:** registro con `status = "Fin Cauciones"` o `"Fin Pases"` para la fecha
- **Monitoreo:** el log muestra todos los registros de Supabase por fecha, incluyendo `alyc` (que ahora indica "ALYC - TipoOp") y `status`
- **Errores esperados:** ALYCs sin boletos para la fecha muestran `status = "Error - Halted Exception: Nothing could be found for the search"` — es normal

Script principal: `run_boletos_zapier.py` — procesa fechas de Drive en orden inverso (más reciente primero), hasta 5 en paralelo.

Script específico por ALYC: `run_da_zapier.py` — dispara Zapier solo para DAValores, secuencial, espera `status` con "Fin" (10 min max por fecha).

**Tabla `procesamiento_boletos` (Supabase):** registra cada PDF descargado con campos `id, fecha_operacion, alyc, tipo, nro_boleto, filename, drive_file_id, fecha_descarga`. Se inserta desde `supabase_logger.py` llamado por `main.py` y `batch_download.py` tras cada upload exitoso a Drive.

**Ejecución automática diaria:** `run_daily.sh` croneado con `0 12 * * 1-6` (lunes a sábado, 9 AM Argentina). Procesa los últimos 2 días hábiles con `batch_download.py`, luego lanza Zapier con `run_boletos_zapier.py`. Usa `xvfb-run` para scrapers headless=False.

## Scripts de utilidad

- `run_puente_retry.py` — retry Puente desde fecha hardcodeada hasta hoy
- `run_puente_backfill.py` — backfill Puente rango completo
- `run_bacs_backfill.py`, `run_bacs_gap_retry.py` — backfill/retry BACS
- `run_criteria_backfill.py` — backfill Criteria
- `run_conosur_ene.py`, `run_conosur_fix_retry_mn.py`, `run_conosur_pases_fix.py`
- `run_maxcapital_mar.py`
- `run_adcap_ene.py`
- `run_da_backfill.py` — backfill DA Valores desde 2026-01-15 hasta hoy (reverse order); 66 boletos en 19 fechas
- `run_da_zapier.py` — dispara Zapier solo para DAValores para fechas con PDFs en `downloads/DAValores/`
- `cleanup_metro_pases_drive.py`, `cleanup_conosur_pases_drive.py` — limpieza de archivos subidos incorrectamente a Drive
- `cleanup_puente_nombres_drive.py` — mueve a papelera en Drive archivos de Puente con nombre de idMovimiento (patrón `16xxxxxx`)
- `run_puente_fix_nombres.py` — re-descarga Puente para fechas con nombres incorrectos y limpia Drive
- `upload_cocos_pases.py` — procesa zip de boletos Cocos, extrae nro de boleto del PDF y sube a Drive
- `supabase_logger.py` — registra PDFs descargados en tabla `procesamiento_boletos`
- `run_daily.sh` — script de cron diario (descarga + Zapier)

## Notas técnicas

- Los scrapers usan `playwright` con `async_playwright`
- Puente usa **persistent context** (perfil en `browser_profiles/puente/`) para mantener sesión entre ejecuciones
- `main.py` fuerza `headless=True` en producción; algunos scrapers tienen override `headless=false` en config
- Variables de entorno se expanden con `_resolve_env()` desde el patrón `${VAR}`
- El uploader organiza en Drive por: `root_folder / tipo_operacion / fecha / Boleto - {ALYC} - {NRO}.pdf`
  (el archivo va plano bajo la fecha; no hay subcarpeta por ALYC). Los tipos con
  `tipo_folder_overrides` cuelgan de su carpeta raíz propia en vez de `root / tipo`.

- **Nombre visible de ALYC vs identificador interno:** el `nombre` de `config.json`
  (ej. `DAValores`) es el **identificador interno** — se usa en carpetas locales
  (`downloads/{nombre}/`), en Supabase (`procesamiento_boletos.alyc`) y en Zapier.
  Para el **nombre del archivo en Drive** se traduce con `_ALYC_DRIVE_NAME` en
  `drive_uploader.py` cuando el nombre visible difiere. Hoy solo aplica a
  **`DAValores` → `DA Valores`**. Es el único punto donde se arma el nombre, así que
  la traducción vale para `batch_download`, el reconcile de `daily_orchestrator` y
  `main.py` sin tocar los callers. Convención alineada con
  `jira_controller.FOLDER_TO_JIRA`, que ya mapea el nombre de la ALYC en Jira
  (`DAValores`→`DA Valores`, `WIN`→`Win`, `MaxCapital`→`Max Capital`, `MetroCorp`→`Metrocorp`).
  Si en el futuro se quiere alinear otra ALYC, agregarla a `_ALYC_DRIVE_NAME` **y**
  renombrar los archivos históricos en Drive (si no, al reprocesar una fecha vieja
  el uploader no encuentra el nombre nuevo y crea un duplicado).

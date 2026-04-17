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
| WIN | sistemaC | `alyc_sistemaC.py` | Múltiples cuentas: MeridianoNorte (50015), Pamat (50017) |
| ConoSur | sistemaD | `alyc_sistemaD.py` | Dos instancias: cuenta 3003 (MN) y 3087 (Pamat) |
| MaxCapital | sistemaE | `alyc_sistemaE.py` | headless=false; cuentas MN (20759) y Pamat (20774) |
| MetroCorp | sistemaF | `alyc_sistemaF.py` | Solo Cauciones |
| Dhalmore | sistemaG | `alyc_sistemaG.py` | headless=false; cuentas MN (56553) y Pamat (56555) |

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

## Estado del proyecto (al 2026-03-20)

- Todos los scrapers implementados y funcionando en producción

- **Puente (sistemaA):** se resolvió el problema de seteo de fechas — los inputs `#fechaDesde`/`#fechaHasta` están controlados por AngularJS y `page.fill()` no actualizaba el modelo; se implementó la solución correcta.

- **Puente — nombres de archivo corregidos:** los boletos se guardaban con el `idMovimiento` de la URL (ej: `16437291.pdf`) en lugar del número de boleto real. Se corrigió leyendo el header `Content-Disposition` del response de descarga (ej: `filename="13841 - Movimiento 9304.pdf"` → se guarda como `9304.pdf`). Se re-descargaron y re-subieron las 38 fechas afectadas (15-ene a 12-mar-2026) con `run_puente_fix_nombres.py`, y se eliminaron los ~158 archivos viejos de Drive con `cleanup_puente_nombres_drive.py`.

- **DA Valores (sistemaB):** agregado 2026-03-20. Mismo portal VBhome/Unisync que ADCAP. Solo cuenta MeridianoNorte. Backfill completo: 66 boletos en 19 fechas (2026-02-23 → 2026-03-19). Zapier procesado para todas esas fechas con `run_da_zapier.py` (19/19 OK, `status=Fin Cauciones`).

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
- El uploader organiza en Drive por: `root_folder / tipo_operacion / fecha / alyc_nombre / archivo.pdf`

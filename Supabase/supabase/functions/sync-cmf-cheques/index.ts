// supabase/functions/sync-cmf-cheques/index.ts
//
// Monitorea los cheques/eCheqs que están en tenencia de Meridiano en CMF/COELSA,
// los sincroniza en Supabase y (opcional) da de alta los nuevos en Jira CHEQ.
//
// Pensada para dos disparadores sobre el MISMO endpoint:
//   • horario   → Jira automation "scheduled" (o Zapier) cada 1 hora
//   • a demanda → botón / automation manual en Jira
//
// Flujo:
//   1. Trae de CMF los cheques con tenencia = CUIT Meridiano y el estado pedido.
//      OJO: CMF no permite filtrar por endosos/cesiones, así que la detección de
//      novedades es un DIFF de la tenencia contra nuestra base.
//   2. Deriva la cadena (endosos + cesiones unificados) y de ahí la vía de ingreso
//      y la CONTRAPARTE (de quién lo recibimos = nuestro cliente, no el librador).
//   3. Upsert en procesamiento_cheques + cheques_transmisiones (idempotente).
//   4. Agrupa los nuevos en cheques_operaciones por (cliente, día de ingreso) —
//      esa es la unidad del issue padre "Cesion de Cheques" y del CSV para Doors.
//   5. Si crearJira=true: crea el padre y los hijos en CHEQ, en estado Recepcionado.
//
// Body / query (todo opcional):
//   estado      "ACTIVO" (default) | "DEPOSITADO" | ...
//   desde/hasta AAAAMMDD — acota la ventana de fecha_pago (para corridas parciales)
//   crearJira   true → además del sync, da de alta en Jira (default false)
//   dryRun      true → resuelve todo y no escribe nada
//   maxIssues   tope de issues a crear en una corrida (default 50, freno de seguridad)
//   delta       true → solo lo modificado desde la última corrida (modo del disparo
//               horario); margenHoras (6) y topePaginas (15) lo acotan
//   soloJira    true → alta en Jira desde Supabase, SIN tocar CMF
//   probeJira   true → diagnóstico read-only del token de Jira
//   auditarJira true → read-only: issues y padres que Supabase no referencia
//   sinEstado   true → mira toda la tenencia sin filtrar por estado (para novedades)
//   enviarNovedades true → solo drena la cola de novedades pendientes
//   repesca     true → barrido completo para detectar los que se fueron de la tenencia
//
// Env vars (Supabase Function secrets):
//   SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY   (inyectadas)
//   CMF_GATEWAY_URL          endpoint de cmf-echeq (default = cmf-gateway-prd)
//   CMF_INTERNAL_KEY         x-internal-key del gateway (la key "PROD", incluso para sbx)
//   JIRA_BASE_URL / ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN / ATLASSIAN_WORKSPACE_ID
//   CHEQUES_SYNC_WEBHOOK_SECRET  (opcional) si está, se exige firma HMAC X-Hub-Signature

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const SUPA_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPA_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const CMF_URL = Deno.env.get("CMF_GATEWAY_URL") ??
  "https://func-cmf-gateway-prd-eyfpggcfh5c4argj.chilecentral-01.azurewebsites.net/api/cmf-echeq";
const CMF_KEY = Deno.env.get("CMF_INTERNAL_KEY") ?? "";
const JIRA_BASE_URL = Deno.env.get("JIRA_BASE_URL") ?? "https://meridianonorte.atlassian.net";
const ATLASSIAN_EMAIL = Deno.env.get("ATLASSIAN_EMAIL") ?? "";
const ATLASSIAN_API_TOKEN = Deno.env.get("ATLASSIAN_API_TOKEN") ?? "";
const ASSETS_WORKSPACE_ID = Deno.env.get("ATLASSIAN_WORKSPACE_ID") ?? "";
const WEBHOOK_SECRET = Deno.env.get("CHEQUES_SYNC_WEBHOOK_SECRET") ?? "";

const MERIDIANO_CUIT = "30707418849";
const PAGE_SIZE = 20;        // tope de CMF para el nodo cheques
const MAX_INTENTOS = 5;
// CMF rate-limitea fuerte. Medido el 2026-08-20 sobre 15 páginas con el select
// completo: concurrencia 2 → 14 OK / 1 rechazo; concurrencia 3 → 3 OK / 12 rechazos;
// concurrencia 5 → 0 OK / 15 rechazos (429). El techo lo pone CMF, no nosotros.
const PAGINAS_EN_PARALELO = 2;
const CLAIM_TTL_MIN = 5;     // un claim más viejo que esto es de un isolate muerto

// $orderby solo acepta campos de fecha. fecha_ult_modif además es casi única
// (1307 valores distintos en 1308 cheques, empate máximo 2), así que como clave
// de orden no sufre el solapamiento de páginas que sí tiene fecha_pago.
const ORDEN_PAGO = "cheques.fecha_pago!";
const ORDEN_MODIF = "cheques.fecha_ult_modif!";

// Estado de COELSA -> tipo de novedad. Los tres últimos se registran igual, pero
// NO mueven el issue: la decisión de qué transición corresponde vive en Jira, y
// para esos tres no hay ninguna que sea correcta sin mirarlos a mano.
//   CADUCADO             se venció la ventana de 31 días; ambiguo entre garantía,
//                        precancelado y depósito que se pasó.
//   DEVOLUCION-PENDIENTE devolución del endoso en curso; puede volver a ACTIVO.
//   REPUDIADO            cheques que emitimos nosotros y rebotaron; no son cartera.
const TIPO_POR_ESTADO: Record<string, string> = {
  DEPOSITADO: "DEPOSITADO",
  // PRESENTADO es la etapa siguiente del clearing (depositado -> presentado al banco
  // girado -> pagado/rechazado) y para Jira es el mismo estado de negocio. Medido:
  // 50/50 con cbu_deposito y vencidos, igual que DEPOSITADO. Mapean al mismo tipo, y
  // como el unique es (cheque_id, tipo) el cheque genera UNA sola novedad aunque pase
  // por los dos.
  PRESENTADO: "DEPOSITADO",
  PAGADO: "ACREDITADO",
  RECHAZADO: "RECHAZADO",
  CADUCADO: "CADUCADO",
  "DEVOLUCION-PENDIENTE": "DEVOLUCION_PENDIENTE",
  REPUDIADO: "REPUDIADO",
};

// Estados desde los que un cheque todavía puede cambiar. Son los que hay que
// seguir mirando, y los únicos que vale la pena repescar si se van de la tenencia.
// OJO: esta lista NO es cerrada. El 2026-08-14 los estados sumaban exacto el total
// con 7 valores y PRESENTADO no existía; apareció el 25/08 con 50 cheques. Un estado
// desconocido que llegue acá se trata como final y el cheque se descarta como
// historia, así que conviene revisar `descartados_historia` cuando salta.
const ESTADOS_NO_FINALES = ["ACTIVO", "DEPOSITADO", "PRESENTADO", "DEVOLUCION-PENDIENTE"];

// Fecha en la que arrancó el flujo. Un cheque que no conocemos y que YA viene
// terminado igual es cartera si ENTRÓ después de esta fecha: son los que pasaron por
// nosotros y se cobraron antes de que el sync llegara a verlos.
//
// Filtrar solo por estado no alcanzaba. Medido el 2026-09-03 sobre dos ventanas de
// la puesta al día: de 77 cheques descartados por estado final, 73 ($431 M) habían
// entrado despues del 11/08 y eran cartera legítima.
const INICIO_FLUJO = "2026-08-11";

// ── Jira CHEQ ──────────────────────────────────────────────────────────────────
const PROJECT_KEY = "CHEQ";
const IT_ECHEQ = "10419";    // issue type ECheq
const IT_FISICO = "10168";   // issue type Cheque (físicos)
const IT_PADRE = "10199";    // Cesion de Cheques
const CF = {
  moneda: "customfield_10127",
  banco: "customfield_10157",           // Assets objectType 15
  nroCheque: "customfield_10454",
  cuentaBancaria: "customfield_10553",
  librador: "customfield_10720",        // Assets objectType 84
  fechaEmision: "customfield_10722",
  fechaVencimiento: "customfield_10723",
  importe: "customfield_10725",
  estadoClearing: "customfield_10727",
  noALaOrden: "customfield_10728",
  sucursal: "customfield_11005",
  endososCesiones: "customfield_12314",   // párrafo (ADF): la cadena con orden y fechas
  endosantes: "customfield_12315",        // Assets multi-valor: quiénes intervinieron
  // padre
  cliente: "customfield_10154",         // Assets objectType 8 (Entidades)
  cantidadCheques: "customfield_10730",
  fechaCesion: "customfield_10764",
};
const OT_ENTIDADES = "8";
const OT_LIBRADORES = "84";
const OT_BANCOS = "15";
const MONEDAS: Record<string, string> = { "032": "ARS", "080": "USD" };

const CAMPOS_CMF = [
  "cheque_id", "cmc7", "cheque_numero", "numero_chequera",
  "cuenta_emisora.banco_codigo", "cuenta_emisora.banco_nombre",
  "cuenta_emisora.sucursal_codigo", "cuenta_emisora.emisor_cuit",
  "cuenta_emisora.emisor_razon_social", "cuenta_emisora.emisor_cuenta",
  "cuenta_emisora.emisor_moneda",
  "cheque_tipo", "cheque_caracter", "cheque_modo", "cheque_concepto",
  "estado", "monto", "fecha_emision", "fecha_pago", "fecha_pago_vencida",
  "fecha_ult_modif", "cbu_deposito", "cedido", "cesion_pendiente",
  "es_ultimo_endosante", "beneficiario_final_documento", "beneficiario_final_nombre",
  "emitido_a.beneficiario_documento", "emitido_a.beneficiario_nombre",
  "tenencia.beneficiario_documento", "tenencia.beneficiario_nombre",
  "endosos", "cesiones", "rechazos",
];
const SELECT = CAMPOS_CMF.map((c) => "cheques." + c).join(",");

const json = (status: number, obj: unknown) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
const txt = (v: unknown) => (v == null ? null : String(v).trim() || null);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function hmacSha256Hex(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function safeEqual(a: string, b: string) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

// ── CMF ────────────────────────────────────────────────────────────────────────

async function cmfPagina(estado: string | null, pagina: number, desde?: string, hasta?: string,
                         select?: string, orderby: string = ORDEN_PAGO) {
  // estado null = toda la tenencia sin filtrar. Es lo que necesita el delta de
  // novedades: filtrando por ACTIVO nunca veríamos el cambio a DEPOSITADO.
  const cond = [
    `cheques.tenencia.beneficiario_documento eq __${MERIDIANO_CUIT}__`,
  ];
  if (estado) cond.push(`cheques.estado eq __${estado}__`);
  if (desde) cond.push(`cheques.fecha_pago ge ${desde}`);
  if (hasta) cond.push(`cheques.fecha_pago le ${hasta}`);

  const res = await fetch(CMF_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-internal-key": CMF_KEY },
    body: JSON.stringify({
      select: select ?? SELECT,
      filter: cond.join(" and "),
      orderby,
      pag: `cheques:${pagina}-${PAGE_SIZE}`,
    }),
  });
  const body = await res.json().catch(() => ({}));
  // El 429 de CMF viene con el body VACÍO, así que sin este chequeo explícito cae
  // en el error genérico como "CMF null: null" y parece que CMF se cayó.
  if (body?.cmf_status === 429) {
    const e = new Error(`CMF 429 (rate limit) en la página ${pagina}`);
    (e as any).rateLimit = true;
    throw e;
  }
  if (body?.cmf_code !== "2400") {
    throw new Error(`CMF ${body?.cmf_code}: ${String(body?.cmf_description).slice(0, 200)}`);
  }
  return {
    filas: (body?.data?.cheques ?? []) as any[],
    total: (body?.data?.total_cheques ?? null) as number | null,
  };
}

/**
 * Reintenta ante páginas vacías y errores intermitentes.
 *
 * Distingue **"CMF contestó cero"** de **"CMF no contestó"**: si ningún intento
 * obtuvo respuesta válida, tira. Devolver la página vacía como si el universo
 * fuera cero hace que con CMF caído el sync responda `ok: true` y
 * "universo vacío", y que el disparo horario informe "sin novedades" para
 * siempre. Es el mismo modo de falla silenciosa que costó WIN, MetroCorp y
 * Allaria: el caso real fue el 502 de CMF del 2026-08-20 12:56.
 */
async function cmfPaginaRetry(estado: string | null, pagina: number, desde?: string, hasta?: string,
                              select?: string, orderby: string = ORDEN_PAGO) {
  let total: number | null = null;
  let contesto = false;
  let ultimoError = "";
  for (let i = 1; i <= MAX_INTENTOS; i++) {
    let esperar = 1200 * i;
    try {
      const r = await cmfPagina(estado, pagina, desde, hasta, select, orderby);
      contesto = true;
      if (r.total != null) total = r.total;
      if (r.filas.length || total === 0) return { filas: r.filas, total };
    } catch (e) {
      ultimoError = e instanceof Error ? e.message : String(e);
      // Ante rate limit hay que aflojar de verdad: reintentar rápido lo empeora,
      // porque el reintento cuenta contra la misma cuota que ya se pasó.
      if ((e as any)?.rateLimit) esperar = 4000 * i;
    }
    await sleep(esperar);
  }
  if (!contesto) {
    throw new Error(`CMF no contestó la página ${pagina} tras ${MAX_INTENTOS} intentos` +
                    (ultimoError ? `: ${ultimoError}` : ""));
  }
  // Contestó pero la página quedó vacía con total > 0. Acá no se tira: el conteo
  // de la ventana lo detecta, la marca INCOMPLETA y el handler devuelve 502.
  return { filas: [] as any[], total };
}

/**
 * Trae una ventana de fecha_pago y VALIDA el conteo contra su propio total_cheques.
 *
 * Por qué: la paginación de CMF solapa filas cuando la clave de orden (fecha_pago)
 * empata, así que paginar el universo entero de corrido pierde registros
 * (1241 de 1255 en la prueba). Si la ventana no cierra, se parte al medio: con
 * menos páginas el solapamiento desaparece.
 */
async function ventana(estado: string | null, desde: string, hasta: string,
                       acc: Map<string, any>, log: string[], prof = 0): Promise<void> {
  const { total } = await cmfPaginaRetry(estado, 1, desde, hasta, "cheques.cheque_id");
  if (!total) return;

  const vistos = new Map<string, any>();
  const paginas = Math.ceil(total / PAGE_SIZE);
  // De a PAGINAS_EN_PARALELO. Secuencial no entra en el isolate: el barrido
  // completo son ~63 páginas × ~1,9 s y dio 504 a los 151 s (límite 150 s).
  // No se arregla pidiendo menos — una página tarda lo mismo trayendo solo el
  // cheque_id que trayendo el select entero, así que el costo es latencia por
  // request y lo único que lo baja es solaparlas.
  for (let p = 1; p <= paginas; p += PAGINAS_EN_PARALELO) {
    const lote = [];
    for (let q = p; q < p + PAGINAS_EN_PARALELO && q <= paginas; q++) {
      lote.push(cmfPaginaRetry(estado, q, desde, hasta));
    }
    for (const { filas } of await Promise.all(lote)) {
      for (const c of filas) if (c?.cheque_id) vistos.set(c.cheque_id, c);
    }
  }
  for (const [k, v] of vistos) acc.set(k, v);

  if (vistos.size === total) {
    log.push(`${desde}-${hasta}: ${vistos.size}/${total} OK`);
    return;
  }
  const d0 = new Date(`${desde.slice(0, 4)}-${desde.slice(4, 6)}-${desde.slice(6, 8)}T00:00:00Z`);
  const d1 = new Date(`${hasta.slice(0, 4)}-${hasta.slice(4, 6)}-${hasta.slice(6, 8)}T00:00:00Z`);
  if (d0 >= d1 || prof >= 12) {
    log.push(`${desde}-${hasta}: ${vistos.size}/${total} INCOMPLETA (faltan ${total - vistos.size})`);
    return;
  }
  log.push(`${desde}-${hasta}: ${vistos.size}/${total} -> parto`);
  const medio = new Date((d0.getTime() + d1.getTime()) / 2);
  const f = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, "");
  const sig = new Date(medio.getTime() + 86400000);
  await ventana(estado, desde, f(medio), acc, log, prof + 1);
  await ventana(estado, f(sig), hasta, acc, log, prof + 1);
}

/**
 * Marca de agua del delta. Vive en su propia tabla, NO en max(fecha_ult_modif).
 *
 * Sacarla de los datos tiene una trampa: si el delta no llega a cubrir todo el
 * hueco, el upsert de los cheques más nuevos empuja ese max hacia adelante y el
 * ciclo siguiente arranca desde ahí — el tramo del medio no se mira nunca más.
 * Verificado el 2026-08-25: con la marca en el 10/08, 25 páginas llegaron al 22/08
 * y quedó incompleto; escribir eso habría perdido 12 días de cambios.
 */
async function marcaDeAgua(): Promise<string | null> {
  const [fila] = (await supa("cheques_sync_estado?select=ultima_modif&id=eq.1")) ?? [];
  if (fila?.ultima_modif) return fila.ultima_modif;
  // Bootstrap: si la tabla está vacía se arranca de lo que haya cargado.
  const filas: any[] = await supa(
    "procesamiento_cheques?select=fecha_ult_modif&fecha_ult_modif=not.is.null" +
    "&order=fecha_ult_modif.desc&limit=1");
  return filas?.[0]?.fecha_ult_modif ?? null;
}

/** Avanza la marca. Se llama SOLO cuando el delta cerró completo. */
async function avanzarMarca(hasta: string | null) {
  if (!hasta) return;
  await supa("cheques_sync_estado?on_conflict=id", {
    method: "POST",
    body: JSON.stringify([{ id: 1, ultima_modif: hasta,
                            ultima_corrida: new Date().toISOString(),
                            actualizado_at: new Date().toISOString() }]),
    headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
  });
}

/**
 * Modo DELTA: lo que cambió desde la última corrida. Es el camino del disparo
 * horario, y el que hace que el sync entre cómodo en los 150 s del isolate.
 *
 * CMF **no deja filtrar** por fecha_ult_modif (`$filter` da 2499 campo
 * incorrecto), pero **sí deja ordenar** por ese campo. Así que en vez de
 * "traeme lo nuevo" se pide todo ordenado por última modificación descendente y
 * se corta al cruzar la marca de agua: en una corrida horaria son 1 o 2 páginas
 * en lugar de las ~63 del barrido completo.
 *
 * El margen hacia atrás cubre que la marca de agua se guarda por cheque y no por
 * corrida; releer de más no cuesta nada porque el upsert es idempotente. Lo que
 * sí importa es no leer de menos.
 */
async function traerDelta(estado: string | null, margenHoras: number, topePaginas: number) {
  const acc = new Map<string, any>();
  const log: string[] = [];
  const marca = await marcaDeAgua();
  const corteMs = marca ? Date.parse(marca) - margenHoras * 3600_000 : null;
  log.push(`marca de agua: ${marca ?? "(base vacía)"}` +
           (corteMs ? ` | corte: ${new Date(corteMs).toISOString()} (margen ${margenHoras} h)` : ""));

  let alcanzado = false;
  let maxVisto: string | null = null;
  for (let p = 1; p <= topePaginas; p++) {
    const { filas } = await cmfPaginaRetry(estado, p, undefined, undefined, undefined, ORDEN_MODIF);
    if (!filas.length) {
      log.push(`pag ${p}: vacía, se acabó el universo`);
      alcanzado = true;
      break;
    }
    for (const c of filas) if (c?.cheque_id) acc.set(c.cheque_id, c);
    // La página 1 trae lo más nuevo: ese es el punto al que avanzará la marca si
    // el delta cierra completo.
    if (p === 1) maxVisto = aHoraArg(filas[0]?.fecha_ult_modif);
    const ultima = aHoraArg(filas[filas.length - 1]?.fecha_ult_modif);
    const ultimaMs = ultima ? Date.parse(ultima) : NaN;
    log.push(`pag ${p}: ${filas.length} filas, hasta ${String(ultima).slice(0, 19)}`);
    if (corteMs != null && Number.isFinite(ultimaMs) && ultimaMs < corteMs) {
      log.push(`corte alcanzado en la página ${p}`);
      alcanzado = true;
      break;
    }
  }
  // Sin marca de agua (base vacía) no hay con qué cortar: lo que se trajo es un
  // recorte arbitrario del universo, no un delta.
  if (!marca) log.push("DELTA SIN MARCA: la base está vacía, correr el barrido completo");
  else if (!alcanzado) log.push(`DELTA INCOMPLETO: se agotaron las ${topePaginas} páginas sin llegar al corte`);
  return { cheques: [...acc.values()], total: acc.size, log, marca, maxVisto,
           deltaIncompleto: !alcanzado || !marca };
}

async function traerTodos(estado: string | null, desde?: string, hasta?: string) {
  const acc = new Map<string, any>();
  const log: string[] = [];

  if (desde && hasta) {
    await ventana(estado, desde, hasta, acc, log);
    return { cheques: [...acc.values()], total: acc.size, log };
  }

  // rango completo: min/max fecha_pago -> ventanas mensuales
  const primera = await cmfPaginaRetry(estado, 1, undefined, undefined, "cheques.fecha_pago");
  const total = primera.total ?? 0;
  if (!total) return { cheques: [], total: 0, log: ["universo vacío"] };
  const ultima = await cmfPaginaRetry(estado, Math.ceil(total / PAGE_SIZE), undefined, undefined,
                                      "cheques.fecha_pago");
  const fechas = [...primera.filas, ...ultima.filas]
    .map((f) => String(f.fecha_pago ?? "").slice(0, 10)).filter(Boolean).sort();
  let cur = new Date(`${fechas[0].slice(0, 7)}-01T00:00:00Z`);
  const fin = new Date(`${fechas[fechas.length - 1]}T00:00:00Z`);
  const f = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, "");
  while (cur <= fin) {
    const prox = new Date(Date.UTC(cur.getUTCFullYear(), cur.getUTCMonth() + 1, 1));
    await ventana(estado, f(cur), f(new Date(prox.getTime() - 86400000)), acc, log);
    cur = prox;
  }
  return { cheques: [...acc.values()], total, log };
}

// ── derivación de la cadena ────────────────────────────────────────────────────

type Eslabon = {
  tipo: "ENDOSO" | "CESION"; orden: number; fecha: string | null;
  estado: string | null; estado_norm: string; subtipo: string | null;
  origen_cuit: string; origen_nombre: string | null;
  destino_cuit: string; destino_nombre: string | null;
  cesion_id: string | null; motivo_repudio: string | null; es_deposito: boolean; raw: unknown;
};

function cadena(c: any): Eslabon[] {
  const out: any[] = [];
  for (const e of c.endosos ?? []) {
    out.push({
      tipo: "ENDOSO", fecha: e.fecha_hora ?? null, estado: e.estado_endoso ?? null,
      subtipo: e.tipo_endoso ?? null,
      origen_cuit: String(e.emisor_documento ?? "").trim(),
      origen_nombre: txt(e.emisor_razon_social),
      destino_cuit: String(e.benef_documento ?? "").trim(),
      destino_nombre: txt(e.benef_razon_social),
      cesion_id: null, motivo_repudio: txt(e.motivo_repudio), raw: e,
    });
  }
  for (const x of c.cesiones ?? []) {
    out.push({
      tipo: "CESION", fecha: x.fecha_emision_cesion ?? null, estado: x.estado_cesion ?? null,
      subtipo: null,
      origen_cuit: String(x.cedente_documento ?? "").trim(),
      origen_nombre: txt(x.cedente_nombre),
      destino_cuit: String(x.cesionario_documento ?? "").trim(),
      destino_nombre: txt(x.cesionario_nombre),
      cesion_id: x.cesion_id ?? null, motivo_repudio: txt(x.cesion_motivo_repudio), raw: x,
    });
  }
  out.sort((a, b) => String(a.fecha ?? "").localeCompare(String(b.fecha ?? "")));
  return out.map((e, i) => ({
    ...e, orden: i + 1,
    estado_norm: String(e.estado ?? "").toUpperCase().replace("ACEPTADA", "ACEPTADO"),
    es_deposito: e.origen_cuit === MERIDIANO_CUIT && e.destino_cuit === MERIDIANO_CUIT,
  }));
}

/** Último eslabón que nos trajo el cheque: destino nosotros, origen un tercero. */
function ingreso(esl: Eslabon[]) {
  const c = esl.filter((e) => e.destino_cuit === MERIDIANO_CUIT &&
                              e.origen_cuit !== MERIDIANO_CUIT &&
                              (e.estado_norm === "ACEPTADO" || e.estado_norm === ""));
  return c.length ? c[c.length - 1] : null;
}

const ddmmyyyy = (iso: string | null) =>
  !iso ? "" : `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(0, 4)}`;

/**
 * CMF/COELSA manda timestamps SIN zona ("2026-03-02T09:35:42") que son hora
 * Argentina. Se les pega el offset -03:00 explícito para no depender del
 * TimeZone de la sesión de Postgres al insertar.
 *
 * Argentina no usa horario de verano desde 2009, así que -03:00 es fijo.
 * El instante resultante es el mismo que ya tienen las filas cargadas, así que
 * el `on conflict` de cheques_transmisiones sigue matcheando y no duplica.
 */
const HORA_ARG = "-03:00";
const aHoraArg = (naive: string | null | undefined) => {
  const s = String(naive ?? "").trim();
  if (!s) return null;
  return /(?:Z|[+-]\d{2}:?\d{2})$/.test(s) ? s : `${s}${HORA_ARG}`;
};

/**
 * Los cheques DIRECTO no tienen eslabón que marque el ingreso, así que se usa la
 * fecha de emisión. Se fija al MEDIODIA hora Argentina a propósito: a medianoche,
 * cualquier conversión de zona la corre al día anterior y el cheque cae en otra
 * operación (ya pasó: 44 cheques quedaron con ingreso 21:00 del día previo).
 */
const ingresoDirecto = (fechaEmision: unknown) => {
  const d = String(fechaEmision ?? "").slice(0, 10);
  return d ? `${d}T12:00:00${HORA_ARG}` : null;
};

/**
 * Párrafo para customfield_12314, con el vocabulario del CSV de Doors.
 *
 * Devuelve ADF (Atlassian Document Format), no texto: en la API v3 los campos
 * `textarea` lo exigen. Mandar string da
 * "Operation value must be an Atlassian Document".
 */
function renderParrafo(esl: Eslabon[]): Record<string, unknown> {
  const lineas = !esl.length
    ? ["Sin endosos ni cesiones (emitido directamente a Meridiano)."]
    : esl.map((e) => {
      const verbo = e.tipo === "ENDOSO" ? "Endosado por" : "Cedido por";
      const hora = e.fecha ? ` ${e.fecha.slice(11, 16)}` : "";
      const dep = e.es_deposito ? "  [depósito]" : "";
      const est = e.estado_norm && e.estado_norm !== "ACEPTADO" ? `  [${e.estado_norm}]` : "";
      return `${e.orden}. ${ddmmyyyy(e.fecha)}${hora} ${verbo} ${e.origen_nombre ?? "?"}` +
             ` CUIT - ${e.origen_cuit} → ${e.destino_nombre ?? "?"} (${e.destino_cuit})${dep}${est}`;
    });
  return {
    type: "doc",
    version: 1,
    content: lineas.map((text) => ({ type: "paragraph", content: [{ type: "text", text }] })),
  };
}

function cabecera(c: any) {
  const ce = c.cuenta_emisora ?? {};
  const ea = c.emitido_a ?? {};
  const esl = cadena(c);
  const ing = ingreso(esl);
  const car = String(c.cheque_caracter ?? "").trim().toLowerCase();
  const emitidoANosotros = String(ea.beneficiario_documento ?? "").trim() === MERIDIANO_CUIT;

  // Emitido a nuestro nombre y sin ningún eslabón de un TERCERO hacia nosotros:
  // el librador nos lo emitió directo, y ahí la contraparte ES el librador.
  // No se puede pedir cadena vacía: si ya lo depositamos hay un endoso
  // Meridiano → Meridiano que no es un ingreso.
  const directo = !ing && emitidoANosotros;

  return {
    fila: {
      cheque_id: c.cheque_id,
      cmc7: txt(c.cmc7),
      cheque_numero: txt(c.cheque_numero),
      numero_chequera: txt(c.numero_chequera),
      tipo: "ECHEQ",
      cheque_caracter: txt(c.cheque_caracter),
      no_a_la_orden: car ? car.startsWith("no a la orden") : null,
      librador: txt(ce.emisor_razon_social),
      librador_cuit: txt(ce.emisor_cuit),
      banco_codigo: txt(ce.banco_codigo),
      banco_nombre: txt(ce.banco_nombre),
      cuenta_emisora: txt(ce.emisor_cuenta),
      sucursal: txt(ce.sucursal_codigo),
      cbu_deposito: txt(c.cbu_deposito),
      beneficiario_nombre: txt(ea.beneficiario_nombre),
      beneficiario_cuit: txt(ea.beneficiario_documento),
      moneda: MONEDAS[String(ce.emisor_moneda ?? "").trim()] ?? "ARS",
      monto: c.monto ?? null,
      fecha_emision: String(c.fecha_emision ?? "").slice(0, 10) || null,
      fecha_pago: String(c.fecha_pago ?? "").slice(0, 10) || null,
      fecha_ult_modif: aHoraArg(c.fecha_ult_modif),
      fecha_ingreso: ing ? aHoraArg(ing.fecha) : (directo ? ingresoDirecto(c.fecha_emision) : null),
      via_ingreso: ing ? ing.tipo : (directo ? "DIRECTO" : null),
      contraparte_nombre: ing ? ing.origen_nombre : (directo ? txt(ce.emisor_razon_social) : null),
      contraparte_cuit: ing ? ing.origen_cuit : (directo ? txt(ce.emisor_cuit) : null),
      estado: txt(c.estado),
      estado_norm: String(c.estado ?? "").trim().toUpperCase() || null,
      cantidad_endosos: (c.endosos ?? []).length,
      cantidad_cesiones: (c.cesiones ?? []).length,
      raw: c,
    },
    eslabones: esl,
  };
}

// ── Supabase REST ──────────────────────────────────────────────────────────────

async function supa(path: string, init: RequestInit = {}) {
  const res = await fetch(`${SUPA_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SUPA_KEY, Authorization: `Bearer ${SUPA_KEY}`,
      "Content-Type": "application/json", ...(init.headers ?? {}),
    },
  });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const t = await res.text();
  return t ? JSON.parse(t) : null;
}

async function upsert(tabla: string, filas: any[], onConflict: string, resolution: string) {
  for (let i = 0; i < filas.length; i += 200) {
    await supa(`${tabla}?on_conflict=${onConflict}`, {
      method: "POST",
      body: JSON.stringify(filas.slice(i, i + 200)),
      headers: { Prefer: `resolution=${resolution},return=minimal` },
    });
  }
}

// ── Assets ─────────────────────────────────────────────────────────────────────
//
// Se resuelve con `normalize-asset` (la misma que usa el flujo de Facturas/FCE):
// hace matching fuzzy por trigramas, desempata con GPT si el score es dudoso y
// crea el objeto si se le pide. No duplicamos esa lógica acá.

type AssetRes = { objectId: string | null; created: boolean; needsReview: boolean };

/**
 * Caché por (objectType, valor) para toda la vida del isolate.
 *
 * Importa: `normalize-asset` desempata con GPT cuando el score es dudoso, así que sin
 * caché se paga ese desempate una vez por cheque. Hay intervinientes que aparecen en
 * decenas de cheques (uno solo en 48), y muchos cheques comparten librador y banco.
 */
const assetCache = new Map<string, AssetRes>();

async function assetObjectId(
  value: string | null,
  objectTypeId: string,
  opts: {
    attributeName?: string;
    crear?: boolean;
    createAttributes?: Record<string, unknown>;
  } = {},
): Promise<AssetRes> {
  if (!value) return { objectId: null, created: false, needsReview: false };
  const ck = `${objectTypeId}|${opts.attributeName ?? "CUIT"}|${value}`;
  const hit = assetCache.get(ck);
  if (hit) return hit;
  const res = await fetch(`${SUPA_URL}/functions/v1/normalize-asset`, {
    method: "POST",
    headers: { Authorization: `Bearer ${SUPA_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      objectTypeId,
      attributeName: opts.attributeName ?? "CUIT",
      value,
      buscar_alias: true,
      crear_si_no_match: opts.crear === true,
      ...(opts.createAttributes ? { create_attributes: opts.createAttributes } : {}),
    }),
  });
  const b = await res.json().catch(() => ({}));
  const out: AssetRes = {
    objectId: b?.match?.objectId ? String(b.match.objectId) : null,
    created: b?.created === true,
    needsReview: b?.needs_review === true,
  };
  if (out.objectId) assetCache.set(ck, out);   // no cachear los fallos
  return out;
}

/**
 * Terceros de la cadena, sin repetir y en orden de aparición: van al campo
 * Assets `Endosantes` (multi-valor). Nosotros quedamos afuera.
 *
 * Usa el MISMO objectType que `Librador` (84) a propósito: la misma empresa cumple
 * roles distintos según el cheque (vimos un cliente actuando de endosante intermedio
 * en otros 7), así que un solo objeto por CUIT y el rol lo da el campo que lo apunta.
 * Dos catálogos paralelos obligarían a deduplicar a mano para siempre.
 */
async function resolverEndosantes(esl: Eslabon[]) {
  const cuits: { cuit: string; nombre: string | null }[] = [];
  for (const e of esl) {
    if (e.origen_cuit && e.origen_cuit !== MERIDIANO_CUIT &&
        !cuits.some((c) => c.cuit === e.origen_cuit)) {
      cuits.push({ cuit: e.origen_cuit, nombre: e.origen_nombre });
    }
  }
  const ids: string[] = [];
  for (const c of cuits) {
    const a = await assetObjectId(c.cuit, OT_LIBRADORES, {
      crear: true,
      createAttributes: { Nombre: c.nombre ?? c.cuit, CUIT: c.cuit },
    });
    if (a.objectId) ids.push(a.objectId);
  }
  return ids;
}

const assetsRef = (objectId: string) => [{
  workspaceId: ASSETS_WORKSPACE_ID,
  id: `${ASSETS_WORKSPACE_ID}:${objectId}`,
  objectId: String(objectId),
}];

// ── Jira ───────────────────────────────────────────────────────────────────────

function jiraAuth() {
  return "Basic " + btoa(`${ATLASSIAN_EMAIL}:${ATLASSIAN_API_TOKEN}`);
}

/**
 * Crea el issue reintentando ante 429 y 5xx.
 *
 * La carga inicial son ~485 padres + ~1300 hijos y Jira responde con
 * `x-ratelimit-limit: 200`, así que sin esto la corrida se corta a mitad de camino
 * y deja los cheques a medio crear.
 */
async function jiraCreate(fields: Record<string, unknown>) {
  const MAX = 6;
  for (let intento = 1; ; intento++) {
    const res = await fetch(`${JIRA_BASE_URL}/rest/api/3/issue`, {
      method: "POST",
      headers: { Authorization: jiraAuth(), "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ fields }),
    });
    if ((res.status === 429 || res.status >= 500) && intento < MAX) {
      const retryAfter = Number(res.headers.get("retry-after") ?? 0);
      await res.body?.cancel();
      await sleep(retryAfter > 0 ? retryAfter * 1000 : 1500 * intento * intento);
      continue;
    }
    const b = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`Jira create ${res.status}: ${JSON.stringify(b).slice(0, 300)}`);
    return b as { id: string; key: string };
  }
}

/** Busca en Jira paginando con nextPageToken. */
async function jiraBuscar(jql: string, campos: string, tope = 5000) {
  const out: any[] = [];
  let token: string | null = null;
  do {
    const u = new URL(`${JIRA_BASE_URL}/rest/api/3/search/jql`);
    u.searchParams.set("jql", jql);
    u.searchParams.set("fields", campos);
    u.searchParams.set("maxResults", "100");
    if (token) u.searchParams.set("nextPageToken", token);
    const r = await fetch(u, { headers: { Authorization: jiraAuth(), Accept: "application/json" } });
    if (!r.ok) throw new Error(`Jira search ${r.status}: ${(await r.text()).slice(0, 200)}`);
    const d = await r.json();
    out.push(...(d.issues ?? []));
    token = d.nextPageToken ?? null;
  } while (token && out.length < tope);
  return out;
}

const cfNum = (id: string) => id.replace("customfield_", "");

/**
 * Busca un padre ya creado para la operación por su **clave natural**: día de
 * cesión + cliente. Está verificado que `(contraparte_cuit, día)` es único en las
 * 485 operaciones, así que identifica la operación sin ambigüedad.
 *
 * Cubre el caso "el padre se creó en Jira pero el PATCH a Supabase no llegó":
 * sin esto la corrida siguiente crea un SEGUNDO padre y la operación queda
 * partida entre los dos. Pasó el 2026-08-11 con AG LIFT SA 2026-08-07, cuyos
 * 4 cheques quedaron 2 en CHEQ-2882 y 2 en CHEQ-2883.
 */
async function buscarPadreExistente(op: any, clienteId: string | null) {
  const dia = String(op.dia ?? "").slice(0, 10);
  if (!dia) return null;
  const candidatos = await jiraBuscar(
    `project = ${PROJECT_KEY} AND issuetype = "Cesion de Cheques"` +
    ` AND cf[${cfNum(CF.fechaCesion)}] = "${dia}"`,
    `key,summary,${CF.cliente}`);
  const nombre = String(op.contraparte_nombre ?? op.contraparte_cuit ?? "");
  for (const c of candidatos) {
    const cli = c.fields?.[CF.cliente];
    const objId = Array.isArray(cli) && cli.length ? String(cli[0]?.objectId ?? "") : "";
    // Con el Cliente resuelto se compara contra el objeto de Assets. Si la Entidad
    // todavía no existe, se cae al nombre del summary, que la función arma siempre igual.
    if (clienteId && objId) {
      if (objId === String(clienteId)) return { key: c.key, id: String(c.id) };
    } else if (nombre && String(c.fields?.summary ?? "").startsWith(`${nombre} —`)) {
      return { key: c.key, id: String(c.id) };
    }
  }
  return null;
}

/** Crea el issue padre "Cesion de Cheques" de una operación, o adopta el que ya exista. */
async function crearPadre(op: any) {
  // El Cliente NO se crea automáticamente: las Entidades las da de alta riesgos
  // vía Pipedrive. Si falta, el padre queda sin Cliente y eso es la señal.
  const clienteId = op.cliente_asset_id ??
    (await assetObjectId(op.contraparte_cuit, OT_ENTIDADES)).objectId;
  const yaEsta = await buscarPadreExistente(op, clienteId);
  if (yaEsta) return { issue: yaEsta, clienteId, adoptado: true };
  const fields: Record<string, unknown> = {
    project: { key: PROJECT_KEY },
    issuetype: { id: IT_PADRE },
    summary: `${op.contraparte_nombre ?? op.contraparte_cuit} — ${op.cantidad_cheques} cheque(s) ${op.dia}`,
    [CF.fechaCesion]: op.dia,
    [CF.cantidadCheques]: op.cantidad_cheques,
    [CF.moneda]: { value: op.moneda ?? "ARS" },
  };
  if (clienteId) fields[CF.cliente] = assetsRef(clienteId);
  const issue = await jiraCreate(fields);
  return { issue, clienteId, adoptado: false };
}

/** Crea el issue del cheque, colgado del padre (o suelto si parentKey es null). */
async function crearCheque(ch: any, parentKey: string | null, esl: Eslabon[]) {
  const parrafo = renderParrafo(esl);
  const endosantes = await resolverEndosantes(esl);
  // Librador SÍ se crea si no existe: es un catálogo de Nombre + CUIT y el librador
  // no pasa por análisis de riesgo (a diferencia del Cliente/Entidad).
  const [librador, banco] = await Promise.all([
    assetObjectId(ch.librador_cuit, OT_LIBRADORES, {
      crear: true,
      createAttributes: { Nombre: ch.librador ?? ch.librador_cuit, CUIT: ch.librador_cuit },
    }),
    assetObjectId(ch.banco_codigo, OT_BANCOS, { attributeName: "Código BCRA" }),
  ]);
  const libradorId = librador.objectId;
  const bancoId = banco.objectId;
  const fields: Record<string, unknown> = {
    project: { key: PROJECT_KEY },
    issuetype: { id: ch.tipo === "FISICO" ? IT_FISICO : IT_ECHEQ },
    ...(parentKey ? { parent: { key: parentKey } } : {}),
    summary: `${ch.tipo === "FISICO" ? "CHEQUE" : "ECHEQ"} N° ${ch.cheque_numero}` +
             ` - ${ch.librador ?? "?"} - ${ch.moneda} ${ch.monto}`,
    [CF.nroCheque]: ch.cheque_numero,
    [CF.importe]: Number(ch.monto),
    [CF.moneda]: { value: ch.moneda ?? "ARS" },
    [CF.endososCesiones]: parrafo,
    [CF.estadoClearing]: ch.estado ?? null,
  };
  if (ch.fecha_emision) fields[CF.fechaEmision] = ch.fecha_emision;
  if (ch.fecha_pago) fields[CF.fechaVencimiento] = ch.fecha_pago;
  if (ch.cuenta_emisora) fields[CF.cuentaBancaria] = ch.cuenta_emisora;
  if (ch.sucursal) fields[CF.sucursal] = ch.sucursal;
  if (ch.no_a_la_orden != null) {
    fields[CF.noALaOrden] = { value: ch.no_a_la_orden ? "No A La Orden" : "A La Orden" };
  }
  if (libradorId) fields[CF.librador] = assetsRef(libradorId);
  if (bancoId) fields[CF.banco] = assetsRef(bancoId);
  if (endosantes.length) {
    fields[CF.endosantes] = endosantes.flatMap((id) => assetsRef(id));
  }
  return await jiraCreate(fields);
}

// ── alta en Jira de lo pendiente ───────────────────────────────────────────────

async function altaJira(maxIssues: number, dryRun: boolean) {
  // operaciones con cheques pendientes de alta
  const pendientes: any[] = await supa(
    "procesamiento_cheques?select=id,cheque_id,cheque_numero,tipo,monto,moneda,librador," +
    "librador_cuit,banco_codigo,cuenta_emisora,sucursal,no_a_la_orden,fecha_emision,fecha_pago," +
    // jira_issue_key is.null es una GUARDA, no un adorno: sin eso, si algún día se
    // resetea fecha_procesamiento en un update (que es lo que hace el patrón de la
    // casa en sync-invoitrade-echeq-novedades) estas filas se re-crearían como
    // issues nuevos y tendríamos duplicados.
    // estado_norm acotado a la CARTERA. Un barrido sin filtro de estado trae la
    // historia (PAGADO, RECHAZADO, CADUCADO viejos) que nunca estuvo en Jira; sin
    // esto, la primera corrida con crearJira=true les crearía issues retroactivos.
    // Jira registra lo que está en cartera, no lo que ya terminó su ciclo.
    "estado,operacion_id&fecha_procesamiento=is.null&operacion_id=not.is.null" +
    "&jira_issue_key=is.null" +
    `&or=(estado_norm.in.(${ESTADOS_NO_FINALES.map((e) => `"${e}"`).join(",")})` +
    `,fecha_ingreso.gte.${INICIO_FLUJO})` +
    `&order=fecha_ingreso.asc&limit=${maxIssues}`);
  if (!pendientes.length) return { creados: 0, padres: 0, detalle: [] as unknown[] };

  const opIds = [...new Set(pendientes.map((p) => p.operacion_id))];
  const ops: any[] = await supa(
    `cheques_operaciones?select=*&id=in.(${opIds.join(",")})`);
  const opPorId = new Map(ops.map((o) => [o.id, o]));

  const detalle: unknown[] = [];
  let creados = 0, padres = 0;

  for (const opId of opIds) {
    const op = opPorId.get(opId);
    if (!op) continue;
    const delGrupo = pendientes.filter((p) => p.operacion_id === opId);

    if (dryRun) {
      detalle.push({ operacion: op.contraparte_nombre, dia: op.dia, cheques: delGrupo.length,
                     padre: op.jira_parent_key ?? "(a crear)" });
      continue;
    }

    // 1. padre (si no lo tiene todavía).
    //    Si falla, los cheques se crean igual SUELTOS y se les cuelga el padre
    //    después: perder un cheque es peor que dejarlo sin agrupar.
    let parentKey: string | null = op.jira_parent_key ?? null;
    if (!parentKey) {
      // CLAIM ATÓMICO. Sin esto, dos corridas concurrentes (el botón a demanda
      // solapado con la horaria, o un lote que reintenta mientras el isolate
      // anterior sigue vivo) leen jira_parent_key is null a la vez y crean DOS
      // padres. Postgres serializa el UPDATE, así que de dos isolates uno se
      // lleva la fila y el otro recibe 0 filas y no crea nada.
      const vencido = new Date(Date.now() - CLAIM_TTL_MIN * 60_000).toISOString();
      const tomadas: any[] = await supa(
        `cheques_operaciones?id=eq.${opId}&jira_parent_key=is.null` +
        `&or=(jira_parent_claim.is.null,jira_parent_claim.lt.${encodeURIComponent(vencido)})`,
        {
          method: "PATCH",
          body: JSON.stringify({ jira_parent_claim: new Date().toISOString() }),
          headers: { Prefer: "return=representation" },
        }) ?? [];

      if (!tomadas.length) {
        // Otra corrida lo está creando. Se relee por si ya lo dejó escrito.
        const [fresca] = (await supa(
          `cheques_operaciones?select=jira_parent_key&id=eq.${opId}`)) ?? [];
        parentKey = fresca?.jira_parent_key ?? null;
        if (!parentKey) {
          detalle.push({
            operacion: op.contraparte_nombre, dia: op.dia,
            nota: "padre en curso en otra corrida; cheques creados sueltos",
          });
        }
      } else {
        try {
          const { issue, clienteId, adoptado } = await crearPadre(op);
          parentKey = issue.key;
          if (!adoptado) padres++;
          await supa(`cheques_operaciones?id=eq.${opId}`, {
            method: "PATCH",
            body: JSON.stringify({
              jira_parent_key: issue.key, jira_parent_id: issue.id,
              cliente_asset_id: clienteId, fecha_procesamiento: new Date().toISOString(),
            }),
            headers: { Prefer: "return=minimal" },
          });
          if (adoptado) {
            detalle.push({ operacion: op.contraparte_nombre, dia: op.dia,
                           padre_adoptado: issue.key });
          }
        } catch (e) {
          // Se suelta el claim para que el próximo ciclo reintente sin esperar el TTL.
          await supa(`cheques_operaciones?id=eq.${opId}`, {
            method: "PATCH",
            body: JSON.stringify({ jira_parent_claim: null }),
            headers: { Prefer: "return=minimal" },
          }).catch(() => {});
          detalle.push({
            operacion: op.contraparte_nombre, dia: op.dia,
            padre_error: e instanceof Error ? e.message : String(e),
            nota: "cheques creados sin padre; se recuelgan en el próximo ciclo",
          });
        }
      }
    }

    // 2. hijos
    for (const ch of delGrupo) {
      try {
        const esl: any[] = await supa(
          `cheques_transmisiones?select=*&cheque_id=eq.${ch.cheque_id}&order=orden.asc`);
        const issue = await crearCheque(ch, parentKey, esl as Eslabon[]);
        await supa(`procesamiento_cheques?id=eq.${ch.id}`, {
          method: "PATCH",
          body: JSON.stringify({
            jira_issue_key: issue.key, jira_issue_id: issue.id,
            fecha_procesamiento: new Date().toISOString(),
          }),
          headers: { Prefer: "return=minimal" },
        });
        creados++;
        detalle.push({ cheque_id: ch.cheque_id, issue: issue.key, padre: parentKey });
      } catch (e) {
        detalle.push({ cheque_id: ch.cheque_id, error: e instanceof Error ? e.message : String(e) });
      }
    }
  }
  return { creados, padres, detalle };
}

// ── novedades ────────────────────────────────────────────────────────────────
//
// La función es un SENSOR: detecta el cambio de estado, lo registra y dispara un
// webhook por tipo. NO transiciona el issue. Del otro lado hay una automation de
// Jira por tipo, así la lógica de qué transición corresponde vive junto al
// workflow y se cambia sin redeployar.

type Novedad = {
  cheque_id: string; cheque_numero: string | null; jira_issue_key: string | null;
  tipo: string; estado_anterior: string | null; estado_nuevo: string | null;
  detalle: Record<string, unknown>;
};

/**
 * Compara lo que trajo CMF contra lo que ya tenemos.
 *
 * TIENE que correr ANTES del upsert: el upsert pisa `estado`, y después del pisón
 * ya no hay contra qué comparar.
 *
 * Un cheque que no está en la base todavía no es novedad — es un alta, y de eso se
 * ocupa altaJira.
 */
async function previosDe(ids: string[]): Promise<Map<string, any>> {
  const previos = new Map<string, any>();
  for (let i = 0; i < ids.length; i += 200) {
    const lote = ids.slice(i, i + 200);
    const filas: any[] = await supa(
      `procesamiento_cheques?select=cheque_id,estado_norm,jira_issue_key` +
      `&cheque_id=in.(${lote.join(",")})`);
    for (const f of filas ?? []) previos.set(f.cheque_id, f);
  }
  return previos;
}

/**
 * Un cheque que NO conocemos y que YA viene en estado final nunca pasó por nuestra
 * cartera: es historia de COELSA que arrastra el barrido sin filtro de estado.
 * Guardarlo solo ensucia la tabla y le genera una operación que jamás va a tener
 * padre en Jira (el 2026-08-25 entraron 127 cheques y 62 operaciones así).
 */
const esCartera = (fila: any, previos: Map<string, any>) =>
  previos.has(fila.cheque_id) ||
  ESTADOS_NO_FINALES.includes(String(fila.estado_norm ?? "")) ||
  String(fila.fecha_ingreso ?? "") >= INICIO_FLUJO;

async function detectarNovedades(cabeceras: any[],
                                 eslabonesPorCheque: Map<string, Eslabon[]>,
                                 previos: Map<string, any>): Promise<Novedad[]> {
  if (!cabeceras.length) return [];
  const out: Novedad[] = [];
  for (const c of cabeceras) {
    const prev = previos.get(c.cheque_id);
    if (!prev) continue;                                   // alta, no novedad
    const base = {
      cheque_id: c.cheque_id, cheque_numero: c.cheque_numero,
      jira_issue_key: prev.jira_issue_key ?? null,
      detalle: { monto: c.monto, fecha_pago: c.fecha_pago, librador: c.librador,
                 contraparte: c.contraparte_nombre } as Record<string, unknown>,
    };

    if (prev.estado_norm !== c.estado_norm) {
      const tipo = TIPO_POR_ESTADO[String(c.estado_norm ?? "")];
      // ACTIVO no genera novedad: es el estado de reposo, no un evento.
      if (tipo) {
        out.push({ ...base, tipo, estado_anterior: prev.estado_norm,
                   estado_nuevo: c.estado_norm });
      }
    }

    // El cheque se fue de la cartera: hay un endoso NUESTRO hacia un tercero que no es
    // el depósito. Se exige ACEPTADO — el único endoso nuestro hacia afuera que hay en
    // la base está REPUDIADO con motivo "error", o sea que nunca ocurrió.
    //
    // Esto NO decide que se vendió. La venta se detecta cuando llega el BOLETO de la
    // ALyC, igual que con los FCE: COELSA solo muestra que el cheque salió, no por qué
    // (endosarle el cheque a un proveedor para pagarle también lo saca). Por eso la
    // novedad es informativa y no mueve el issue; sirve para explicar por qué el cheque
    // dejó de aparecer y para corroborar la venta cuando llegue el boleto.
    const salida = (eslabonesPorCheque.get(c.cheque_id) ?? []).find(
      (e) => e.origen_cuit === MERIDIANO_CUIT && e.destino_cuit !== MERIDIANO_CUIT &&
             !e.es_deposito && (e.estado_norm === "ACEPTADO" || e.estado_norm === ""));
    if (salida) {
      out.push({ ...base, tipo: "SALIO_DE_CARTERA",
                 estado_anterior: prev.estado_norm, estado_nuevo: c.estado_norm,
                 detalle: { ...base.detalle, destino: salida.destino_nombre,
                            destino_cuit: salida.destino_cuit, fecha: salida.fecha } });
    }
  }
  return out;
}

/** Registra las novedades. El unique (cheque_id, tipo) hace que reprocesar no duplique. */
async function registrarNovedades(novedades: Novedad[]) {
  if (!novedades.length) return 0;
  let nuevas = 0;
  for (let i = 0; i < novedades.length; i += 200) {
    const filas = await supa("cheques_novedades?on_conflict=cheque_id,tipo", {
      method: "POST",
      body: JSON.stringify(novedades.slice(i, i + 200)),
      headers: { Prefer: "resolution=ignore-duplicates,return=representation" },
    });
    nuevas += (filas ?? []).length;
  }
  return nuevas;
}

/**
 * Drena la cola: por cada novedad pendiente con webhook activo, POST y marca.
 *
 * Un tipo sin URL o con activo=false se deja pendiente a propósito — así se prende
 * de a un tipo por vez sin perder las novedades de los demás.
 */
async function enviarNovedades(tope: number) {
  const destinos = new Map<string, string>();
  for (const w of (await supa("cheques_novedades_webhooks?select=tipo,url,activo")) ?? []) {
    if (w.activo && w.url) destinos.set(w.tipo, w.url);
  }
  if (!destinos.size) {
    return { enviadas: 0, fallidas: 0, sin_destino: 0, detalle: [] as unknown[] };
  }

  const pendientes: any[] = await supa(
    `cheques_novedades?select=*&enviado_at=is.null&order=detectado_at.asc&limit=${tope}`) ?? [];

  const detalle: unknown[] = [];
  let enviadas = 0, fallidas = 0, sinDestino = 0;
  for (const n of pendientes) {
    const url = destinos.get(n.tipo);
    if (!url) { sinDestino++; continue; }
    let status = 0, error = "";
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          issueKey: n.jira_issue_key, chequeId: n.cheque_id, chequeNumero: n.cheque_numero,
          tipo: n.tipo, estadoAnterior: n.estado_anterior, estadoNuevo: n.estado_nuevo,
          detalle: n.detalle,
          // Jira automation toma el issue del array `issues` del incoming webhook.
          issues: n.jira_issue_key ? [n.jira_issue_key] : [],
        }),
      });
      status = r.status;
      await r.body?.cancel();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
    const ok = status >= 200 && status < 300;
    await supa(`cheques_novedades?id=eq.${n.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        enviado_at: ok ? new Date().toISOString() : null,
        intentos: (n.intentos ?? 0) + 1,
        http_status: status || null,
        error_msg: ok ? null : (error || `HTTP ${status}`),
      }),
      headers: { Prefer: "return=minimal" },
    });
    if (ok) enviadas++; else fallidas++;
    detalle.push({ id: n.id, tipo: n.tipo, issue: n.jira_issue_key, status,
                   error: error || undefined });
  }
  return { enviadas, fallidas, sin_destino: sinDestino, detalle };
}

/** Un cheque puntual, sin filtrar por tenencia. Es la única forma de ver los que se fueron. */
async function cmfPorChequeId(chequeId: string) {
  const res = await fetch(CMF_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-internal-key": CMF_KEY },
    body: JSON.stringify({
      select: SELECT,
      filter: `cheques.cheque_id eq __${chequeId}__`,
      orderby: ORDEN_MODIF,
      pag: "cheques:1-1",
    }),
  });
  const body = await res.json().catch(() => ({}));
  if (body?.cmf_code !== "2400") return null;
  return (body?.data?.cheques ?? [])[0] ?? null;
}

/**
 * REPESCA: los cheques que se fueron de nuestra tenencia.
 *
 * Un cheque negociado deja de estar en la tenencia y por lo tanto desaparece del
 * delta — no genera novedad, simplemente se esfuma. Para verlos hay que barrer los
 * ids de la tenencia y comparar contra los nuestros que siguen en estado no final.
 *
 * Se barre ordenando por fecha_ult_modif (casi única) y NO por fecha_pago: con
 * fecha_pago las páginas se solapan y los faltantes serían falsos positivos.
 *
 * No va en el ciclo de 15 min: es un barrido completo. Corre una vez por día.
 */
async function repesca(topeConsultas: number) {
  const enTenencia = new Set<string>();
  const log: string[] = [];
  // Se barre SOLO por los estados no finales, NO la tenencia entera.
  //
  // Barrer todo son 4916 cheques = 246 páginas y no entra en los 150 s del isolate
  // (medido el 2026-09-04: ~295 s secuencial). Acotado a los no finales son ~1590
  // cheques = 80 páginas, que de a dos son ~48 s.
  //
  // Y además es lo correcto: lo único que importa acá es si nuestros cheques VIVOS
  // siguen en la tenencia. Uno que está en estado final no se perdió, se liquidó, y
  // de esos cambios ya se ocupa el delta.
  for (const est of ESTADOS_NO_FINALES) {
    // Se calcula cuántas páginas hay y NO se pide ni una de más.
    //
    // Paginar "hasta que venga vacía" cuesta carísimo: cmfPaginaRetry trata la página
    // vacía como algo a reintentar (CMF devuelve vacíos intermitentes de verdad), así
    // que una página fuera de rango se lleva 5 intentos con backoff = ~18 s. Con 4
    // estados eso solo eran ~96 s y la repesca moría en el IDLE_TIMEOUT de 150 s.
    const { filas: primera, total } = await cmfPaginaRetry(
      est, 1, undefined, undefined, "cheques.cheque_id", ORDEN_MODIF);
    if (!total) { log.push(`${est}: 0 en tenencia`); continue; }
    for (const c of primera) if (c?.cheque_id) enTenencia.add(c.cheque_id);
    const paginas = Math.ceil(total / PAGE_SIZE);
    for (let p = 2; p <= paginas; p += PAGINAS_EN_PARALELO) {
      const lote = [];
      for (let q = p; q < p + PAGINAS_EN_PARALELO && q <= paginas; q++) {
        lote.push(cmfPaginaRetry(est, q, undefined, undefined,
                                 "cheques.cheque_id", ORDEN_MODIF));
      }
      for (const { filas } of await Promise.all(lote)) {
        for (const c of filas) if (c?.cheque_id) enTenencia.add(c.cheque_id);
      }
    }
    log.push(`${est}: ${total} en tenencia (${paginas} páginas)`);
  }
  log.push(`tenencia (estados no finales): ${enTenencia.size} cheques distintos`);

  const nuestros: any[] = await supa(
    `procesamiento_cheques?select=cheque_id,cheque_numero,estado_norm,jira_issue_key` +
    `&estado_norm=in.(${ESTADOS_NO_FINALES.map((e) => `"${e}"`).join(",")})`) ?? [];
  const faltantes = nuestros.filter((n) => !enTenencia.has(n.cheque_id));
  log.push(`nuestros no finales: ${nuestros.length} | fuera de la tenencia: ${faltantes.length}`);

  const novedades: Novedad[] = [];
  for (const f of faltantes.slice(0, topeConsultas)) {
    const c = await cmfPorChequeId(f.cheque_id);
    if (!c) { log.push(`${f.cheque_numero}: CMF no lo devuelve`); continue; }
    const { fila, eslabones } = cabecera(c);
    const previos = await previosDe([fila.cheque_id]);
    const [n] = await detectarNovedades([fila], new Map([[fila.cheque_id, eslabones]]), previos);
    if (n) novedades.push(n);
    else log.push(`${f.cheque_numero}: fuera de la tenencia pero sin novedad que registrar`);
  }
  const nuevas = await registrarNovedades(novedades);
  return {
    fuera_de_tenencia: faltantes.length,
    consultados: Math.min(faltantes.length, topeConsultas),
    novedades: novedades.length, nuevas, log,
  };
}


// ── handler ────────────────────────────────────────────────────────────────────

serve(async (req) => {
  if (req.method !== "POST") return json(405, { ok: false, error: "Method not allowed" });

  const rawBody = await req.text();
  if (WEBHOOK_SECRET) {
    const header = req.headers.get("x-hub-signature") ?? "";
    const expected = "sha256=" + await hmacSha256Hex(WEBHOOK_SECRET, rawBody);
    if (!safeEqual(header, expected)) {
      return json(401, { ok: false, error: "Firma X-Hub-Signature inválida o ausente" });
    }
  }
  let body: any = {};
  try { body = rawBody ? JSON.parse(rawBody) : {}; } catch { body = {}; }
  const url = new URL(req.url);
  const p = (k: string) => body[k] ?? url.searchParams.get(k) ?? undefined;

  const estado = String(p("estado") ?? "ACTIVO").toUpperCase();
  const desde = p("desde") ? String(p("desde")) : undefined;
  const hasta = p("hasta") ? String(p("hasta")) : undefined;
  const crearJira = p("crearJira") === true || p("crearJira") === "true";
  const dryRun = p("dryRun") === true || p("dryRun") === "true";
  const maxIssues = Number(p("maxIssues") ?? 50);
  // delta = solo lo modificado desde la última corrida. Es el modo del disparo
  // horario; el barrido completo queda para backfills y para poblar de cero.
  const delta = p("delta") === true || p("delta") === "true";
  const margenHoras = Number(p("margenHoras") ?? 6);
  const topePaginas = Number(p("topePaginas") ?? 15);
  // sinEstado = mirar TODA la tenencia sin filtrar por estado. Es obligatorio para
  // novedades: filtrando por ACTIVO nunca veríamos el pase a DEPOSITADO.
  const sinEstado = p("sinEstado") === true || p("sinEstado") === "true";
  const topeNovedades = Number(p("topeNovedades") ?? 100);

  // Diagnóstico read-only del token de Jira que hay en los secrets: hasta dónde llega.
  // No devuelve el token, solo su prefijo y largo. Sirve para saber si esta cuenta puede
  // crear en CHEQ antes de intentarlo con 1300 issues.
  if (p("probeJira") === true || p("probeJira") === "true") {
    if (!ATLASSIAN_EMAIL || !ATLASSIAN_API_TOKEN) {
      return json(500, { ok: false, error: "Faltan ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN" });
    }
    const get = async (path: string) => {
      const r = await fetch(`${JIRA_BASE_URL}/rest/api/3/${path}`,
        { headers: { Authorization: jiraAuth(), Accept: "application/json" } });
      const t = await r.text();
      let b: unknown = null;
      try { b = JSON.parse(t); } catch { b = t.slice(0, 140); }
      return { status: r.status, body: b as any };
    };
    const [me, proyectos, cheq, perms] = await Promise.all([
      get("myself"),
      get("project/search?maxResults=1"),
      get("project/CHEQ"),
      get("mypermissions?projectKey=CHEQ&permissions=BROWSE_PROJECTS,CREATE_ISSUES,EDIT_ISSUES,TRANSITION_ISSUES"),
    ]);
    return json(200, {
      ok: true,
      email: ATLASSIAN_EMAIL,
      token_prefijo: ATLASSIAN_API_TOKEN.slice(0, 6),
      token_largo: ATLASSIAN_API_TOKEN.length,
      myself: me.status === 200
        ? { displayName: me.body?.displayName, accountId: me.body?.accountId,
            accountType: me.body?.accountType }
        : me,
      proyectos_visibles: proyectos.status === 200 ? proyectos.body?.total : proyectos,
      cheq: cheq.status === 200 ? { key: cheq.body?.key, name: cheq.body?.name } : cheq,
      permisos_cheq: perms.status === 200
        ? Object.fromEntries(Object.entries(perms.body?.permissions ?? {})
            .map(([k, v]) => [k, (v as any)?.havePermission]))
        : perms,
    });
  }

  /**
   * Audita Jira contra Supabase: devuelve los issues de cheque que existen en Jira
   * y NO están referenciados por ninguna fila.
   *
   * Se producen cuando el create en Jira sale bien pero el PATCH a Supabase no llega
   * (timeout del isolate a los 150s): la fila queda pendiente, un lote posterior crea
   * un SEGUNDO issue y el primero queda huérfano. Solo lectura.
   */
  if (p("auditarJira") === true || p("auditarJira") === "true") {
    if (!ATLASSIAN_EMAIL || !ATLASSIAN_API_TOKEN) {
      return json(500, { ok: false, error: "Faltan ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN" });
    }
    // 1. todos los issues de cheque en Jira
    const enJira: { key: string; nro: string; parent: string | null; creado: string }[] = [];
    let token: string | null = null;
    do {
      const u = new URL(`${JIRA_BASE_URL}/rest/api/3/search/jql`);
      u.searchParams.set("jql", `project = ${PROJECT_KEY} AND issuetype in ("ECheq","Cheque") ORDER BY key ASC`);
      u.searchParams.set("fields", `key,parent,created,${CF.nroCheque}`);
      u.searchParams.set("maxResults", "100");
      if (token) u.searchParams.set("nextPageToken", token);
      const r = await fetch(u, { headers: { Authorization: jiraAuth(), Accept: "application/json" } });
      if (!r.ok) return json(502, { ok: false, error: `Jira search ${r.status}` });
      const d = await r.json();
      for (const i of d.issues ?? []) {
        enJira.push({
          key: i.key,
          nro: String(i.fields?.[CF.nroCheque] ?? ""),
          parent: i.fields?.parent?.key ?? null,
          creado: String(i.fields?.created ?? "").slice(0, 19),
        });
      }
      token = d.nextPageToken ?? null;
    } while (token && enJira.length < 5000);

    // 2. todas las keys referenciadas en Supabase
    const refs = new Set<string>();
    for (let off = 0; ; off += 1000) {
      const filas: any[] = await supa(
        `procesamiento_cheques?select=jira_issue_key&jira_issue_key=not.is.null` +
        `&limit=1000&offset=${off}`);
      for (const f of filas) refs.add(f.jira_issue_key);
      if (filas.length < 1000) break;
    }

    const huerfanos = enJira.filter((i) => !refs.has(i.key));
    // agrupar por número de cheque para ver de qué cheque es cada duplicado
    const porNro = new Map<string, string[]>();
    for (const i of enJira) {
      if (!i.nro) continue;
      porNro.set(i.nro, [...(porNro.get(i.nro) ?? []), i.key]);
    }

    // 3. LOS PADRES. Auditarlos aparte importa: el 2026-08-11 quedaron 6 padres
    //    vacíos y 1 con hijos que Supabase no referencia, y esta auditoría no los
    //    veía porque solo miraba issues de cheque. Un padre vacío es basura; un
    //    padre no referenciado con hijos significa una operación PARTIDA en dos.
    const padresJira = await jiraBuscar(
      `project = ${PROJECT_KEY} AND issuetype = "Cesion de Cheques" ORDER BY key ASC`,
      `key,summary,created`);
    const hijosPorPadre = new Map<string, string[]>();
    for (const i of enJira) {
      if (i.parent) hijosPorPadre.set(i.parent, [...(hijosPorPadre.get(i.parent) ?? []), i.key]);
    }
    const refsPadre = new Set<string>();
    for (let off = 0; ; off += 1000) {
      const filas: any[] = await supa(
        `cheques_operaciones?select=jira_parent_key&jira_parent_key=not.is.null` +
        `&limit=1000&offset=${off}`);
      for (const f of filas) refsPadre.add(f.jira_parent_key);
      if (filas.length < 1000) break;
    }
    const resumenPadre = (p: any) => ({
      key: p.key,
      summary: String(p.fields?.summary ?? ""),
      creado: String(p.fields?.created ?? "").slice(0, 19),
      hijos: hijosPorPadre.get(p.key) ?? [],
    });

    return json(200, {
      ok: true,
      en_jira: enJira.length,
      referenciados_en_supabase: refs.size,
      huerfanos: huerfanos.map((h) => ({
        ...h, otros_con_mismo_nro: (porNro.get(h.nro) ?? []).filter((k) => k !== h.key),
      })),
      padres_en_jira: padresJira.length,
      padres_referenciados_en_supabase: refsPadre.size,
      // Sin hijos: se pueden borrar.
      padres_vacios: padresJira.filter((p) => !(hijosPorPadre.get(p.key) ?? []).length)
                               .map(resumenPadre),
      // Con hijos pero fuera de la base: la operación quedó partida entre dos padres.
      // Hay que recolgar esos hijos del padre que sí referencia Supabase.
      padres_no_referenciados: padresJira
        .filter((p) => !refsPadre.has(p.key) && (hijosPorPadre.get(p.key) ?? []).length)
        .map(resumenPadre),
    });
  }

  // Drena la cola de novedades sin volver a consultar CMF. Sirve para reintentar
  // los envíos fallidos y para prender un tipo de webhook nuevo sobre lo ya detectado.
  if (p("enviarNovedades") === true || p("enviarNovedades") === "true") {
    const t = Date.now();
    const envio = await enviarNovedades(topeNovedades);
    return json(200, { ok: true, enviarNovedades: true, ...envio, ms: Date.now() - t });
  }

  // Los cheques que se fueron de la tenencia (negociados). Barrido completo:
  // corre una vez por día, NO en el ciclo de 15 min.
  if (p("repesca") === true || p("repesca") === "true") {
    if (!CMF_KEY) return json(500, { ok: false, error: "Falta env var CMF_INTERNAL_KEY" });
    const t = Date.now();
    // El barrido se lleva ~85 s de los 150 del isolate, así que quedan ~15 consultas
    // por corrida. Si hay más faltantes, se corre varias veces: es idempotente.
    const r = await repesca(Number(p("topeConsultas") ?? 15));
    return json(200, { ok: true, repesca: true, ...r, ms: Date.now() - t });
  }

  // Alta en Jira desde lo que ya está en Supabase, SIN tocar CMF.
  // Es el camino de la carga inicial y el de reintentar sin re-sincronizar.
  if (p("soloJira") === true || p("soloJira") === "true") {
    if (!ATLASSIAN_EMAIL || !ATLASSIAN_API_TOKEN) {
      return json(500, { ok: false, error: "Faltan ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN" });
    }
    const t = Date.now();
    const jira = await altaJira(maxIssues, dryRun);
    return json(200, { ok: true, soloJira: true, dryRun, ...jira, ms: Date.now() - t });
  }

  if (!CMF_KEY) return json(500, { ok: false, error: "Falta env var CMF_INTERNAL_KEY" });
  if (!SUPA_URL || !SUPA_KEY) return json(500, { ok: false, error: "Faltan SUPABASE_URL / SERVICE_ROLE_KEY" });
  if (crearJira && (!ATLASSIAN_EMAIL || !ATLASSIAN_API_TOKEN)) {
    return json(500, { ok: false, error: "crearJira requiere ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN" });
  }

  const t0 = Date.now();
  try {
    // 1-2. traer de CMF y derivar
    const estadoFiltro = sinEstado ? null : estado;
    const traido = delta
      ? await traerDelta(estadoFiltro, margenHoras, topePaginas)
      : await traerTodos(estadoFiltro, desde, hasta);
    const { cheques, total, log } = traido;
    const deltaIncompleto = (traido as { deltaIncompleto?: boolean }).deltaIncompleto === true;
    const todas: any[] = [];
    const eslabonesPorCheque = new Map<string, Eslabon[]>();
    for (const c of cheques) {
      const { fila, eslabones } = cabecera(c);
      todas.push(fila);
      eslabonesPorCheque.set(fila.cheque_id, eslabones);
    }

    // Se descarta la historia que arrastra el barrido sin filtro de estado: cheques
    // que no conocemos y que ya vienen terminados. Ver esCartera().
    const previos = await previosDe(todas.map((c) => c.cheque_id).filter(Boolean));
    const cabeceras = todas.filter((c) => esCartera(c, previos));
    const descartados = todas.length - cabeceras.length;

    const eslabonFilas: any[] = [];
    for (const fila of cabeceras) {
      for (const e of eslabonesPorCheque.get(fila.cheque_id) ?? []) {
        eslabonFilas.push({
          cheque_id: fila.cheque_id, cmc7: fila.cmc7, tipo: e.tipo, orden: e.orden,
          fecha: aHoraArg(e.fecha), estado: e.estado, estado_norm: e.estado_norm, subtipo: e.subtipo,
          origen_cuit: e.origen_cuit || "", origen_nombre: e.origen_nombre,
          destino_cuit: e.destino_cuit || "", destino_nombre: e.destino_nombre,
          cesion_id: e.cesion_id, motivo_repudio: e.motivo_repudio,
          es_deposito: e.es_deposito, raw: e.raw,
        });
      }
    }

    const incompleto = log.some((l) => l.includes("INCOMPLETA"));
    const resumen: Record<string, unknown> = {
      estado: estadoFiltro ?? "(todos)", modo: delta ? "delta" : "completo",
      cmf_total: total, traidos: cabeceras.length, descartados_historia: descartados,
      eslabones: eslabonFilas.length,
      ventanas: log, paginacion_incompleta: incompleto,
      // Un delta incompleto NO frena la escritura: lo que se trajo es correcto y
      // el upsert es idempotente. Pero hay que verlo, porque significa que puede
      // haber quedado algo sin mirar y conviene disparar el barrido completo.
      ...(delta ? { delta_incompleto: deltaIncompleto } : {}),
    };

    // NOVEDADES. Detectar es solo lectura, así que corre también en dryRun: sirve
    // para ver qué se registraría antes de habilitarlo. Comparar TIENE que pasar
    // antes del upsert, porque el upsert pisa `estado`.
    const novedades = await detectarNovedades(cabeceras, eslabonesPorCheque, previos);
    resumen.novedades_detectadas = novedades.length;
    resumen.novedades = novedades.map((n) => ({
      tipo: n.tipo, issue: n.jira_issue_key, cheque: n.cheque_numero,
      de: n.estado_anterior, a: n.estado_nuevo,
    }));

    if (dryRun) {
      resumen.dryRun = true;
      if (crearJira) resumen.jira = await altaJira(maxIssues, true);
      resumen.ms = Date.now() - t0;
      return json(200, { ok: true, ...resumen });
    }

    // Se registran ANTES del upsert: si el upsert falla, la novedad ya quedó
    // anotada. Es idempotente por el unique (cheque_id, tipo).
    resumen.novedades_nuevas = await registrarNovedades(novedades);

    // No escribir si la paginación no cerró: mejor no sincronizar que sincronizar a medias
    if (incompleto) {
      return json(502, { ok: false, error: "La paginación de CMF no cerró; no escribo nada", ...resumen });
    }

    // 3. upsert
    if (cabeceras.length) {
      await upsert("procesamiento_cheques", cabeceras, "cheque_id", "merge-duplicates");
      await upsert("cheques_transmisiones", eslabonFilas,
                   "cheque_id,tipo,fecha,origen_cuit,destino_cuit", "ignore-duplicates");
    }

    // 4. agrupar en operaciones (cliente + día) y linkear
    const agrupacion = await supa("rpc/cheques_recalcular_operaciones", {
      method: "POST", body: JSON.stringify({}),
    });
    resumen.operaciones = agrupacion;

    // 5. avanzar la marca de agua SOLO si el delta cerró completo. Si quedó
    //    incompleto se deja donde está: el ciclo siguiente vuelve a cubrir el mismo
    //    terreno, que es preferible a saltearlo en silencio.
    if (delta) {
      const maxVisto = (traido as { maxVisto?: string | null }).maxVisto ?? null;
      if (!deltaIncompleto && maxVisto) {
        await avanzarMarca(maxVisto);
        resumen.marca_avanzada_a = maxVisto;
      } else {
        resumen.marca_avanzada_a = "no (delta incompleto)";
      }
    }

    // 6. drenar la cola de novedades (los tipos sin webhook activo quedan pendientes)
    resumen.envio_novedades = await enviarNovedades(topeNovedades);

    // 7. alta en Jira (detrás del flag)
    resumen.jira = crearJira ? await altaJira(maxIssues, false) : "omitido (crearJira=false)";
    resumen.ms = Date.now() - t0;
    return json(200, { ok: true, ...resumen });
  } catch (err) {
    return json(500, { ok: false, error: err instanceof Error ? err.message : String(err),
                       ms: Date.now() - t0 });
  }
});

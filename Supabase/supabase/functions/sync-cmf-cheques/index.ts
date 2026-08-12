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

async function cmfPagina(estado: string, pagina: number, desde?: string, hasta?: string,
                         select?: string) {
  const cond = [
    `cheques.tenencia.beneficiario_documento eq __${MERIDIANO_CUIT}__`,
    `cheques.estado eq __${estado}__`,
  ];
  if (desde) cond.push(`cheques.fecha_pago ge ${desde}`);
  if (hasta) cond.push(`cheques.fecha_pago le ${hasta}`);

  const res = await fetch(CMF_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-internal-key": CMF_KEY },
    body: JSON.stringify({
      select: select ?? SELECT,
      filter: cond.join(" and "),
      orderby: "cheques.fecha_pago!",     // $orderby SOLO acepta fechas
      pag: `cheques:${pagina}-${PAGE_SIZE}`,
    }),
  });
  const body = await res.json().catch(() => ({}));
  if (body?.cmf_code !== "2400") {
    throw new Error(`CMF ${body?.cmf_code}: ${String(body?.cmf_description).slice(0, 200)}`);
  }
  return {
    filas: (body?.data?.cheques ?? []) as any[],
    total: (body?.data?.total_cheques ?? null) as number | null,
  };
}

/** CMF devuelve páginas vacías / errores intermitentes: reintenta. */
async function cmfPaginaRetry(estado: string, pagina: number, desde?: string, hasta?: string,
                              select?: string) {
  let total: number | null = null;
  for (let i = 1; i <= MAX_INTENTOS; i++) {
    try {
      const r = await cmfPagina(estado, pagina, desde, hasta, select);
      if (r.total != null) total = r.total;
      if (r.filas.length || total === 0) return { filas: r.filas, total };
    } catch { /* reintenta */ }
    await sleep(1200 * i);
  }
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
async function ventana(estado: string, desde: string, hasta: string,
                       acc: Map<string, any>, log: string[], prof = 0): Promise<void> {
  const { total } = await cmfPaginaRetry(estado, 1, desde, hasta, "cheques.cheque_id");
  if (!total) return;

  const vistos = new Map<string, any>();
  const paginas = Math.ceil(total / PAGE_SIZE);
  for (let p = 1; p <= paginas; p++) {
    const { filas } = await cmfPaginaRetry(estado, p, desde, hasta);
    for (const c of filas) if (c?.cheque_id) vistos.set(c.cheque_id, c);
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

async function traerTodos(estado: string, desde?: string, hasta?: string) {
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

/** Crea el issue padre "Cesion de Cheques" de una operación. */
async function crearPadre(op: any) {
  // El Cliente NO se crea automáticamente: las Entidades las da de alta riesgos
  // vía Pipedrive. Si falta, el padre queda sin Cliente y eso es la señal.
  const clienteId = op.cliente_asset_id ??
    (await assetObjectId(op.contraparte_cuit, OT_ENTIDADES)).objectId;
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
  return { issue, clienteId };
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
    "estado,operacion_id&fecha_procesamiento=is.null&operacion_id=not.is.null" +
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
    //    Si falla, los cheques se crean igual SUELTOS y se les cuelga el padre después:
    //    perder un cheque es peor que dejarlo sin agrupar. Hoy el workflow de
    //    "Cesion de Cheques" tiene un validador en la transición de creación
    //    ("no se puede Aceptar ... sin los cheques correspondientes") que mira los
    //    hijos del propio issue — siempre 0 al crearlo — así que el alta del padre
    //    falla hasta que ese validador se mueva a la transición Aceptar.
    let parentKey: string | null = op.jira_parent_key ?? null;
    if (!parentKey) {
      try {
        const { issue, clienteId } = await crearPadre(op);
        parentKey = issue.key;
        padres++;
        await supa(`cheques_operaciones?id=eq.${opId}`, {
          method: "PATCH",
          body: JSON.stringify({
            jira_parent_key: issue.key, jira_parent_id: issue.id,
            cliente_asset_id: clienteId, fecha_procesamiento: new Date().toISOString(),
          }),
          headers: { Prefer: "return=minimal" },
        });
      } catch (e) {
        detalle.push({
          operacion: op.contraparte_nombre, dia: op.dia,
          padre_error: e instanceof Error ? e.message : String(e),
          nota: "cheques creados sin padre; reasignar cuando el workflow lo permita",
        });
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
    return json(200, {
      ok: true,
      en_jira: enJira.length,
      referenciados_en_supabase: refs.size,
      huerfanos: huerfanos.map((h) => ({
        ...h, otros_con_mismo_nro: (porNro.get(h.nro) ?? []).filter((k) => k !== h.key),
      })),
    });
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
    const { cheques, total, log } = await traerTodos(estado, desde, hasta);
    const cabeceras: any[] = [];
    const eslabonFilas: any[] = [];
    for (const c of cheques) {
      const { fila, eslabones } = cabecera(c);
      cabeceras.push(fila);
      for (const e of eslabones) {
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
      estado, cmf_total: total, traidos: cabeceras.length, eslabones: eslabonFilas.length,
      ventanas: log, paginacion_incompleta: incompleto,
    };

    if (dryRun) {
      resumen.dryRun = true;
      if (crearJira) resumen.jira = await altaJira(maxIssues, true);
      resumen.ms = Date.now() - t0;
      return json(200, { ok: true, ...resumen });
    }

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

    // 5. alta en Jira (detrás del flag)
    resumen.jira = crearJira ? await altaJira(maxIssues, false) : "omitido (crearJira=false)";
    resumen.ms = Date.now() - t0;
    return json(200, { ok: true, ...resumen });
  } catch (err) {
    return json(500, { ok: false, error: err instanceof Error ? err.message : String(err),
                       ms: Date.now() - t0 });
  }
});

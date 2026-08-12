// supabase/functions/rotar-secret-alyc/index.ts
//
// Recibe un issueKey de un ticket "Cambio de Contraseña" (proyecto CONF de Jira)
// y rota en Azure Key Vault los identificadores de acceso de una cuenta, vía la
// función update-secret de cmf-gateway-prd.
//
// Rota DOS valores, ambos OPCIONALES (portales con 3 identificadores:
// usuario + DNI/CUIT + contraseña):
//   • "Nueva contraseña"     (customfield_12211) → secret {Secret ID}          (…-PASSWORD)
//   • "Nuevo Usuario portal" (customfield_12312) → secret {Secret ID}-DOCUMENTO (DNI/CUIT)
// Si un campo viene vacío NO se toca el valor almacenado; si vienen los dos, se
// actualizan los dos. Si vienen ambos vacíos, el ticket se marca como error.
//
// Flujo:
//   1. (opcional) Verifica la firma HMAC-SHA256 del webhook de Jira (X-Hub-Signature).
//   2. Lee el issue de Jira (Basic auth email:token).
//   3. Del campo "Usuario ALyC" (customfield_12178, objeto Assets/CMDB) obtiene el
//      objectId + workspaceId, y del objeto lee "Secret ID" (= nombre del secret).
//   4. Valida los campos de confirmación (cuando vienen cargados).
//   5. POST a update-secret por cada valor no vacío.
//   6. Si se rotó el DNI, sincroniza el atributo "DNI Usuario" del objeto Assets
//      (best-effort: el vault es la fuente para los scrapers).
//   7. Cierre en Jira (best-effort, salvo dryRun):
//        - OK    → comentario + transición a STATUS_OK ("Listo")
//        - error → comentario + transición a STATUS_FAIL ("Contraseña no actualizada")
//
// Llamada desde un webhook de Jira (verify_jwt=false). Toma el issueKey del query
// (?issueKey=...) o del body ({issueKey} / {issue.key} / {key}).
// Flag { "dryRun": true } → resuelve todo pero NO escribe el vault, Assets ni el issue.
//
// Env vars (Supabase Function secrets):
//   JIRA_BASE_URL                     (ej. https://meridianonorte.atlassian.net)
//   ATLASSIAN_EMAIL                   cuenta de servicio con acceso a Jira + Assets
//   ATLASSIAN_API_TOKEN               API token (Basic auth)
//   UPDATE_SECRET_URL                 endpoint de update-secret (default = cmf-gateway-prd)
//   AZURE_UPDATE_SECRET_FUNCTION_KEY  x-functions-key de update-secret (Azure)
//   JIRA_WEBHOOK_UPDATEPASS_SECRET    (opcional) secret del webhook de Jira; si está
//                          seteado, se exige y valida la firma HMAC-SHA256 (X-Hub-Signature)

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const JIRA_BASE_URL = Deno.env.get("JIRA_BASE_URL") ?? "https://meridianonorte.atlassian.net";
const ATLASSIAN_EMAIL = Deno.env.get("ATLASSIAN_EMAIL") ?? "";
const ATLASSIAN_API_TOKEN = Deno.env.get("ATLASSIAN_API_TOKEN") ?? "";
const UPDATE_SECRET_URL = Deno.env.get("UPDATE_SECRET_URL") ??
  "https://func-cmf-gateway-prd-eyfpggcfh5c4argj.chilecentral-01.azurewebsites.net/api/update-secret";
const UPDATE_SECRET_KEY = Deno.env.get("AZURE_UPDATE_SECRET_FUNCTION_KEY") ?? "";
const JIRA_WEBHOOK_SECRET = Deno.env.get("JIRA_WEBHOOK_UPDATEPASS_SECRET") ?? "";

// Campo del issue que apunta al objeto Assets con las credenciales.
// Sirve igual para ALYCs y para bancos: los dos issuetypes usan ESTE MISMO campo;
// lo único que cambia es el AQL del formulario, que carga objetos de
// "Usuarios Alycs" (typeId 119) o de "Usuarios Banco" (typeId 253). Ambos object
// types tienen los atributos "Secret ID" y "DNI Usuario", así que la lógica es la
// misma y no hace falta distinguirlos acá.
const USUARIO_FIELDS = ["customfield_12178"];   // "Usuario ALyC" / "Usuario Banco" (objeto Assets)

const PASSWORD_FIELD = "customfield_12211";         // "Nueva contraseña"
const PASSWORD_CONFIRM_FIELD = "customfield_12212"; // "Confirmar nueva contraseña"
const DNI_FIELD = "customfield_12312";              // "Nuevo Usuario portal" (DNI/CUIT)
const DNI_CONFIRM_FIELD = "customfield_12313";      // "Confirmar nuevo Usuario portal"

const SECRET_ATTR_NAME = "Secret ID";     // atributo Assets con el nombre del secret
const DNI_ATTR_NAME = "DNI Usuario";      // atributo Assets con el DNI vigente

const STATUS_OK = "Listo";                       // transición al terminar OK
const STATUS_FAIL = "Contraseña no actualizada"; // transición al fallar

const ASSETS_BASE = "https://api.atlassian.com/jsm/assets/workspace";

function json(status: number, obj: unknown) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
}

class RotationError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

async function hmacSha256Hex(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

const txt = (v: unknown) => (typeof v === "string" ? v.trim() : v == null ? "" : String(v).trim());

// ── Jira: comentario + transición por nombre de estado ─────────────────────────

async function jiraComment(issueKey: string, auth: string, text: string) {
  await fetch(`${JIRA_BASE_URL}/rest/api/3/issue/${encodeURIComponent(issueKey)}/comment`, {
    method: "POST",
    headers: { Authorization: auth, "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      body: { type: "doc", version: 1, content: [{ type: "paragraph", content: [{ type: "text", text }] }] },
    }),
  });
}

async function jiraTransitionByName(issueKey: string, auth: string, statusName: string) {
  const res = await fetch(`${JIRA_BASE_URL}/rest/api/3/issue/${encodeURIComponent(issueKey)}/transitions`, {
    headers: { Authorization: auth, Accept: "application/json" },
  });
  if (!res.ok) return { ok: false, error: `GET transitions ${res.status}` };
  const { transitions } = await res.json();
  const t = (transitions ?? []).find(
    (x: any) => (x?.to?.name ?? "").toLowerCase() === statusName.toLowerCase() ||
                (x?.name ?? "").toLowerCase() === statusName.toLowerCase(),
  );
  if (!t) return { ok: false, error: `transición a '${statusName}' no disponible desde el estado actual` };
  const pr = await fetch(`${JIRA_BASE_URL}/rest/api/3/issue/${encodeURIComponent(issueKey)}/transitions`, {
    method: "POST",
    headers: { Authorization: auth, "Content-Type": "application/json" },
    body: JSON.stringify({ transition: { id: t.id } }),
  });
  return pr.ok ? { ok: true, id: t.id } : { ok: false, error: `POST transition ${pr.status}` };
}

// Cierre best-effort del issue: no rompe la respuesta si comentar/transicionar falla
async function cerrarIssue(issueKey: string, auth: string, message: string, statusName: string) {
  const out: Record<string, unknown> = {};
  try { await jiraComment(issueKey, auth, message); out.comment = "ok"; }
  catch (e) { out.comment = `error: ${e instanceof Error ? e.message : String(e)}`; }
  try { out.transition = await jiraTransitionByName(issueKey, auth, statusName); }
  catch (e) { out.transition = { ok: false, error: e instanceof Error ? e.message : String(e) }; }
  return out;
}

// ── Assets ─────────────────────────────────────────────────────────────────────

/** Objeto Assets completo (incluye objectType y attributes con sus ids). */
async function getAssetObject(workspaceId: string, objectId: string, auth: string) {
  const url = `${ASSETS_BASE}/${workspaceId}/v1/object/${objectId}?includeAttributes=true`;
  const res = await fetch(url, { headers: { Authorization: auth, Accept: "application/json" } });
  if (!res.ok) {
    throw new RotationError(502, `Assets object ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  return await res.json();
}

function findAttr(obj: any, name: string) {
  const attrs: any[] = Array.isArray(obj?.attributes) ? obj.attributes : [];
  return attrs.find(
    (a) => (a?.objectTypeAttribute?.name ?? "").trim().toLowerCase() === name.toLowerCase(),
  );
}

function attrValue(obj: any, name: string): string {
  return txt(findAttr(obj, name)?.objectAttributeValues?.[0]?.value);
}

/**
 * Escribe el DNI en el atributo "DNI Usuario" del objeto Assets.
 * Best-effort: el vault es la fuente de verdad para los scrapers; esto mantiene
 * el objeto (lo que ve el operador) sincronizado.
 */
async function syncDniEnAssets(
  workspaceId: string, objectId: string, obj: any, dni: string, auth: string,
) {
  // El atributo puede no venir en el objeto si estaba vacío → buscarlo en el objectType
  let attrId = findAttr(obj, DNI_ATTR_NAME)?.objectTypeAttributeId;
  if (!attrId) {
    const otId = obj?.objectType?.id ?? obj?.objectType?.objectTypeId;
    if (!otId) return { ok: false, error: `no pude resolver el objectType para '${DNI_ATTR_NAME}'` };
    const r = await fetch(`${ASSETS_BASE}/${workspaceId}/v1/objecttype/${otId}/attributes`, {
      headers: { Authorization: auth, Accept: "application/json" },
    });
    if (!r.ok) return { ok: false, error: `GET objecttype attributes ${r.status}` };
    const list = await r.json();
    attrId = (Array.isArray(list) ? list : []).find(
      (a: any) => (a?.name ?? "").trim().toLowerCase() === DNI_ATTR_NAME.toLowerCase(),
    )?.id;
    if (!attrId) return { ok: false, error: `el objectType no tiene atributo '${DNI_ATTR_NAME}'` };
  }

  const r = await fetch(`${ASSETS_BASE}/${workspaceId}/v1/object/${objectId}`, {
    method: "PUT",
    headers: { Authorization: auth, "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      attributes: [{ objectTypeAttributeId: String(attrId), objectAttributeValues: [{ value: dni }] }],
    }),
  });
  return r.ok
    ? { ok: true, attributeId: String(attrId) }
    : { ok: false, error: `PUT object ${r.status}: ${(await r.text()).slice(0, 150)}` };
}

// ── Azure update-secret ────────────────────────────────────────────────────────

async function postUpdateSecret(secretName: string, secretValue: string, issueKey: string) {
  const res = await fetch(UPDATE_SECRET_URL, {
    method: "POST",
    headers: { "x-functions-key": UPDATE_SECRET_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ secret_name: secretName, secret_value: secretValue, issueKey }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || (body as any).ok !== true) {
    throw new RotationError(
      502, `update-secret falló para ${secretName} (HTTP ${res.status}): ${JSON.stringify(body).slice(0, 200)}`,
    );
  }
  return body;
}

// ── Núcleo de la rotación (lanza RotationError en fallos manejados) ─────────────

async function rotar(issueKey: string, auth: string, dryRun: boolean) {
  // 1. Traer el issue con los campos que importan
  const campos = [...USUARIO_FIELDS, PASSWORD_FIELD, PASSWORD_CONFIRM_FIELD, DNI_FIELD, DNI_CONFIRM_FIELD].join(",");
  const issueUrl = `${JIRA_BASE_URL}/rest/api/3/issue/${encodeURIComponent(issueKey)}?fields=${campos}`;
  const issueRes = await fetch(issueUrl, { headers: { Authorization: auth, Accept: "application/json" } });
  if (!issueRes.ok) throw new RotationError(502, `Jira issue ${issueRes.status}: ${(await issueRes.text()).slice(0, 200)}`);
  const issue = await issueRes.json();

  // 2. Valores nuevos (ambos opcionales; vacío = no tocar)
  const newPassword = txt(issue.fields?.[PASSWORD_FIELD]);
  const newPasswordConf = txt(issue.fields?.[PASSWORD_CONFIRM_FIELD]);
  const newDni = txt(issue.fields?.[DNI_FIELD]);
  const newDniConf = txt(issue.fields?.[DNI_CONFIRM_FIELD]);

  if (!newPassword && !newDni) {
    throw new RotationError(422, "El ticket no trae 'Nueva contraseña' ni 'Nuevo Usuario portal' — nada que rotar");
  }
  if (newPassword && newPasswordConf && newPassword !== newPasswordConf) {
    throw new RotationError(422, "La contraseña y su confirmación no coinciden");
  }
  if (newDni && newDniConf && newDni !== newDniConf) {
    throw new RotationError(422, "El Usuario portal (DNI/CUIT) y su confirmación no coinciden");
  }

  // 3. Objeto Assets de credenciales → "Secret ID"
  const ref = USUARIO_FIELDS.map((f) => issue.fields?.[f]?.[0]).find((r: any) => r?.objectId && r?.workspaceId);
  if (!ref) {
    throw new RotationError(422, `Issue sin objeto de credenciales (${USUARIO_FIELDS.join(" / ")})`);
  }
  const { workspaceId, objectId } = ref;

  const obj = await getAssetObject(workspaceId, objectId, auth);
  const secretName = attrValue(obj, SECRET_ATTR_NAME);
  if (!secretName) {
    throw new RotationError(422, `Objeto Assets ${objectId} sin atributo '${SECRET_ATTR_NAME}'`);
  }

  // 4. Armar la lista de updates (solo los campos con valor)
  const updates: { campo: string; secret: string; value: string }[] = [];
  if (newPassword) {
    updates.push({ campo: "contraseña", secret: secretName, value: newPassword });
  }
  if (newDni) {
    if (!/-PASSWORD$/i.test(secretName)) {
      throw new RotationError(
        422,
        `No puedo derivar el secret del DNI: 'Secret ID' = '${secretName}' no termina en -PASSWORD`,
      );
    }
    updates.push({
      campo: "Usuario portal (DNI/CUIT)",
      secret: secretName.replace(/-PASSWORD$/i, "-DOCUMENTO"),
      value: newDni,
    });
  }

  if (dryRun) {
    return {
      dryRun: true,
      objectId,
      dni_actual_en_assets: attrValue(obj, DNI_ATTR_NAME) || null,
      updates: updates.map((u) => ({ campo: u.campo, secret: u.secret, value_len: u.value.length })),
    };
  }

  // 5. Escribir en el vault (uno por uno; si falla el primero no sigue)
  for (const u of updates) await postUpdateSecret(u.secret, u.value, issueKey);

  // 6. Sincronizar el DNI en Assets (best-effort)
  let assetsSync: unknown = undefined;
  if (newDni) {
    try {
      assetsSync = await syncDniEnAssets(workspaceId, objectId, obj, newDni, auth);
    } catch (e) {
      assetsSync = { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
  }

  return {
    dryRun: false,
    objectId,
    updates: updates.map((u) => ({ campo: u.campo, secret: u.secret })),
    assetsSync,
  };
}

serve(async (req) => {
  if (req.method !== "POST") return json(405, { ok: false, error: "Method not allowed" });

  const rawBody = await req.text();

  // Firma del webhook de Jira (X-Hub-Signature = "sha256=<hmac>")
  if (JIRA_WEBHOOK_SECRET) {
    const header = req.headers.get("x-hub-signature") ?? "";
    const expected = "sha256=" + await hmacSha256Hex(JIRA_WEBHOOK_SECRET, rawBody);
    if (!safeEqual(header, expected)) {
      return json(401, { ok: false, error: "Firma X-Hub-Signature inválida o ausente" });
    }
  }

  let body: any;
  try { body = rawBody ? JSON.parse(rawBody) : {}; } catch { body = {}; }

  const url = new URL(req.url);
  const issueKey: string | undefined =
    url.searchParams.get("issueKey") ?? body.issueKey ?? body.issue?.key ?? body.key;
  const dryRun: boolean = body.dryRun === true || url.searchParams.get("dryRun") === "true";
  if (!issueKey) return json(400, { ok: false, error: "Falta issueKey (query ?issueKey= o body)" });

  if (!ATLASSIAN_EMAIL || !ATLASSIAN_API_TOKEN) {
    return json(500, { ok: false, error: "Faltan env vars ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN" });
  }
  if (!dryRun && !UPDATE_SECRET_KEY) {
    return json(500, { ok: false, error: "Falta env var AZURE_UPDATE_SECRET_FUNCTION_KEY" });
  }

  const auth = "Basic " + btoa(`${ATLASSIAN_EMAIL}:${ATLASSIAN_API_TOKEN}`);

  try {
    const res = await rotar(issueKey, auth, dryRun);
    if (dryRun) return json(200, { ok: true, issueKey, ...res });

    const detalle = (res.updates as any[]).map((u) => `${u.campo} (${u.secret})`).join(" y ");
    const sync = res.assetsSync as any;
    const nota = sync && sync.ok === false ? `\n⚠️ No se pudo sincronizar '${DNI_ATTR_NAME}' en Assets: ${sync.error}` : "";
    const jira = await cerrarIssue(
      issueKey, auth,
      `✅ Actualizado en Azure Key Vault: ${detalle}.${nota}`,
      STATUS_OK,
    );
    return json(200, { ok: true, issueKey, ...res, jira });
  } catch (err) {
    const status = err instanceof RotationError ? err.status : 500;
    const msg = err instanceof Error ? err.message : String(err);
    let jira: unknown = undefined;
    if (!dryRun) {
      jira = await cerrarIssue(issueKey, auth, `❌ No se pudo actualizar: ${msg}`, STATUS_FAIL);
    }
    return json(status, { ok: false, error: msg, jira });
  }
});

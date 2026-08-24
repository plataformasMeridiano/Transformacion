-- Novedades de COELSA sobre los cheques en cartera.
--
-- La función NO transiciona el issue: detecta el cambio, lo registra acá y dispara
-- un webhook por tipo. Del otro lado hay una automation de Jira por tipo que hace la
-- transición, así la lógica de "qué transición corresponde" vive junto al workflow
-- y se puede cambiar sin redeployar la función.
--
-- La tabla es a la vez auditoría y cola de reintento: si el webhook falla, la fila
-- queda con enviado_at nulo y la vuelve a tomar el ciclo siguiente.
create table if not exists cheques_novedades (
  id                bigint generated always as identity primary key,
  cheque_id         text not null,
  cheque_numero     text,
  jira_issue_key    text,
  tipo              text not null,
  estado_anterior   text,
  estado_nuevo      text,
  detalle           jsonb,
  detectado_at      timestamptz not null default now(),
  enviado_at        timestamptz,
  intentos          int not null default 0,
  http_status       int,
  error_msg         text
);

comment on table cheques_novedades is
  'Un registro por cambio de estado detectado en COELSA. Sensor + cola de reintento: '
  'la función la llena, el webhook por tipo la drena y una automation de Jira transiciona.';

-- Reprocesar una ventana no debe duplicar la novedad. La clave natural es
-- (cheque, tipo de novedad): un cheque pasa una sola vez a cada estado.
create unique index if not exists cheques_novedades_unica
  on cheques_novedades (cheque_id, tipo);

-- Cola: lo pendiente de enviar, más viejo primero.
create index if not exists cheques_novedades_pendientes_idx
  on cheques_novedades (detectado_at)
  where enviado_at is null;

-- Config de destinos. En tabla y no en un secret para poder dar de alta la URL de una
-- automation nueva sin redeployar, y para prender de a un tipo por vez.
create table if not exists cheques_novedades_webhooks (
  tipo        text primary key,
  url         text not null,
  activo      boolean not null default true,
  descripcion text,
  creado_at   timestamptz not null default now()
);

comment on table cheques_novedades_webhooks is
  'Destino por tipo de novedad. Cada fila es el Incoming webhook de una automation de Jira. '
  'Un tipo sin fila (o con activo=false) se registra igual pero no se envía.';

-- Los tres que NO mueven el issue quedan cargados como inactivos a propósito, con el
-- motivo escrito: son los que hay que mirar a mano.
insert into cheques_novedades_webhooks (tipo, url, activo, descripcion) values
  ('CADUCADO', '', false,
   'Vencio la ventana de 31 dias (fecha_pago + 31) y ya no se puede depositar. NO mueve el '
   'issue: es ambiguo entre garantia, precancelado y deposito que se paso. 31 cheques por '
   '$1.118 MM al 2026-08-20.'),
  ('DEVOLUCION_PENDIENTE', '', false,
   'Devolucion del endoso en curso. NO mueve el issue: es transitorio y puede volver a ACTIVO.'),
  ('REPUDIADO', '', false,
   'El beneficiario rechazo el echeq. NO mueve el issue: son cheques que emitimos nosotros, '
   'no cartera.'),
  ('SALIO_DE_CARTERA', '', false,
   'Endoso nuestro a un tercero que NO es Caja de Valores: el cheque se fue de la cartera '
   'pero no se vendio (tipicamente se le pago a un proveedor con el). NO mueve el issue.')
on conflict (tipo) do nothing;

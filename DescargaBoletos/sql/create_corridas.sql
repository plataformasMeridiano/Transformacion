-- Tabla maestra: una fila por ejecución de main.py
create table if not exists corridas (
    id               uuid primary key default gen_random_uuid(),
    fecha_inicio     timestamptz not null default now(),
    fecha_fin        timestamptz,
    estado           text not null default 'corriendo',  -- corriendo | completado | error
    fecha_procesada  date not null,
    alycs_solicitadas text[],        -- null = todas las activas
    total_desc       int not null default 0,
    total_sub        int not null default 0,
    total_err        int not null default 0,
    notas            text
);

-- Tabla detalle: una fila por ALYC por ejecución
create table if not exists corridas_detalle (
    id             uuid primary key default gen_random_uuid(),
    corrida_id     uuid not null references corridas(id) on delete cascade,
    alyc           text not null,
    sistema        text not null,
    fecha_inicio   timestamptz not null default now(),
    fecha_fin      timestamptz,
    estado         text not null default 'corriendo',  -- corriendo | ok | error
    desc_count     int not null default 0,
    sub_count      int not null default 0,
    err_count      int not null default 0,
    error_detalle  text
);

-- Índices útiles
create index if not exists corridas_fecha_idx on corridas (fecha_procesada desc);
create index if not exists corridas_detalle_corrida_idx on corridas_detalle (corrida_id);
create index if not exists corridas_detalle_alyc_idx on corridas_detalle (alyc);

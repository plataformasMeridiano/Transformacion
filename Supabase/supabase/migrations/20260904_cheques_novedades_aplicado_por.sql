-- Quién resolvió cada novedad.
--
-- El flujo definitivo es webhook por tipo: la función detecta, dispara, y una
-- automation de Jira hace la transición. Hasta que esos webhooks existan, un
-- script en la VM (scripts/aplicar_transiciones_cheques.py) mueve los issues
-- directamente con las transiciones provisorias.
--
-- Sin esta columna no hay forma de saber después qué pasó por el atajo y qué por
-- el camino bueno, y el atajo es justamente lo que hay que poder desarmar.

alter table public.cheques_novedades
  add column if not exists aplicado_por text;

comment on column public.cheques_novedades.aplicado_por is
  'Quién resolvió la novedad. null = webhook (el flujo definitivo). '
  '"transicion-directa" = el puente temporal que corre en la VM y transiciona el '
  'issue con las transiciones provisorias, mientras no existan los webhooks. '
  '"ya-en-estado" = el issue ya estaba en el estado destino y no hubo nada que mover. '
  'Sirve para saber, cuando los webhooks estén, qué se movió por el atajo.';

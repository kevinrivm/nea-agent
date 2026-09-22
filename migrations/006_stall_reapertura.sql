-- 006_stall_reapertura.sql — el candado de cierre se reabre y vuelve a contar.
-- Tras la despedida, un mensaje del lead con contenido reabre la conversación.
-- Los contadores del candado (la racha de relleno y el total de mensajes sin
-- avance) cuentan solo los mensajes con id MAYOR a este: al reabrir se pone el
-- id del último mensaje de la conversación, y lo de antes ya no cuenta. Sin
-- esto, el hilo viejo volvía a disparar el cierre en el primer turno. 0 = se
-- cuenta todo (lo de siempre). Idempotente.

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS stall_since_message_id BIGINT NOT NULL DEFAULT 0;

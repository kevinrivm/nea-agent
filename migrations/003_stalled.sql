-- 003_stalled.sql — candado de cierre: marca de cuándo el agente cerró la
-- conversación por no ir a ningún lado. Mientras esté puesta, el agente no
-- contesta el relleno ("gracias", "ok", un emoji); se reabre en cuanto el lead
-- escribe algo con contenido, o con cualquier mensaje pasado el enfriamiento
-- (STALL_COOLDOWN_HOURS). Ver 006 para los contadores. Idempotente.

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS stalled_at TIMESTAMPTZ;

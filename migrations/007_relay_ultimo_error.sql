-- 007_relay_ultimo_error.sql — cuándo falló el relay por última vez.
-- /health lo enseña (junto con cuántos entrantes esperan y desde cuándo) para
-- que el CRM pueda decir si Nea le está llegando o no, sin ir a los logs.
-- Lo pone cada reintento reprogramado (RelayWorker, entrega fallida). El índice
-- parcial deja leer el máximo sin recorrer la tabla, que no se purga.
-- Idempotente.

ALTER TABLE relay_queue
  ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_relay_queue_last_error
  ON relay_queue (last_error_at)
  WHERE last_error_at IS NOT NULL;

-- El envío diferido necesita el despacho que estaba contestando.
--
-- `POST /api/brains/messages` EXIGE el `dispatchId` (contrato §2.1): es lo que
-- le deja al CRM ser idempotente cuando reintenta un despacho, en vez de
-- responderle dos veces al mismo cliente final.
--
-- El turno lo tenía a mano y la fila no lo guardaba, así que cuando el
-- SenderWorker despertaba —quizá horas después, que es justo para lo que
-- existe— su envío se rechazaba con 422 y volvía a la cola. Reintentando
-- indefinidamente algo que no podía funcionar nunca.
--
-- Igual que en 004: cadena vacía, no NULL, para que las filas de siempre —las
-- de una Nea de un solo negocio, que contesta por /api/bot y no manda
-- despacho— sigan comportándose exactamente igual.
ALTER TABLE pending_send
  ADD COLUMN IF NOT EXISTS dispatch_id TEXT NOT NULL DEFAULT '';

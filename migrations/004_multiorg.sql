-- Multi-organización: una Nea atendiendo a varios negocios.
--
-- ## El fallo que esta migración cierra
--
-- `wa_identity` era UNIQUE a secas. Con un solo negocio eso es correcto: una
-- identidad de WhatsApp es una persona, y una persona es una conversación.
--
-- Con varios negocios en la misma Nea deja de serlo. Dos miembros pueden tener
-- al MISMO cliente —la misma persona escribiéndole a dos negocios distintos, o
-- sencillamente dos leads con números que coinciden tras canonicalizar— y con
-- la unicidad global las dos conversaciones se fusionaban en una: el historial
-- de un negocio entraba en el prompt del otro.
--
-- La clave pasa a ser (organización, identidad). Cadena vacía = "sin
-- organización", que es exactamente lo que era antes, así que una instalación
-- de un solo negocio no cambia de comportamiento.
--
-- ## Por qué '' y no NULL
--
-- En Postgres los NULL son distintos entre sí dentro de un índice único, así
-- que UNIQUE(organization_id, wa_identity) con organization_id NULL permitiría
-- duplicados justo en la instalación de siempre — el caso que no debía cambiar.

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT '';
ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS organization_slug TEXT NOT NULL DEFAULT '';

-- El nombre lo pone Postgres al declarar UNIQUE en la columna (001_init).
ALTER TABLE bot_conversation
  DROP CONSTRAINT IF EXISTS bot_conversation_wa_identity_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_conversation_org_identity
  ON bot_conversation (organization_id, wa_identity);

-- El envío diferido también necesita saber de quién es: cuando el worker
-- despierte, tiene que hablarle al CRM con la credencial de ESA organización.
-- Sin esto, un envío que falló en un multi-organización no se podría reintentar
-- nunca, y la garantía de "jamás se descarta" (incidente 2026-08-03) quedaría
-- rota en silencio, que es la peor forma de romperla.
ALTER TABLE pending_send
  ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT '';
ALTER TABLE pending_send
  ADD COLUMN IF NOT EXISTS organization_slug TEXT NOT NULL DEFAULT '';

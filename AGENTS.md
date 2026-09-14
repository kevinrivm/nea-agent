# Nea — Guía para Codex

Microservicio FastAPI del agente de agendamiento para WhatsApp. Recibe el
webhook de WhatsApp (Meta Cloud API), lo releva al CRM
([vocero-crm](https://github.com/kevinrivm/vocero-crm)) y conversa vía OpenAI
— **enviando siempre a través del API del CRM**, nunca directo a Meta.

## Stack

Python 3.11 · FastAPI + uvicorn (:8000, `/health`) · asyncpg + migraciones SQL
idempotentes al arranque · httpx (CRM y OpenAI) · pytest + respx · Docker
(python:3.11-slim).

## Mapa del código

| Quieres cambiar… | Toca… |
|---|---|
| El chasis conductual del agente | `app/prompt.py` (NO relajar los NUNCA) |
| La capa de persona del negocio | `app/profile.py` (CRM → brief local → mínimo) |
| Las acciones del bot | `app/tools.py` + orquestación en `app/turn.py` |
| El contrato con el CRM | `app/crm.py` (espejo del bot gateway de vocero) |
| Vocero multitenant (modo cloud) | `app/dispatch.py` (entrada) · `app/crm_brains.py` (salida) |
| Atender a VARIOS negocios a la vez | `app/multiorg.py` |
| Webhook/firma/dedup/relay | `app/webhook.py` · `app/relay.py` |
| Coalesce y seguimiento | `app/coalesce.py` · `app/followup.py` |
| Tablas | `migrations/*.sql` (idempotentes, aplican al boot) |

## Reglas duras

- **El bot NUNCA llama a graph.facebook.com para enviar.** Todo por
  `POST {CRM}/api/bot/messages`.
- **Los NUNCA del chasis** (inventar, fingir humano, jerga, datos sensibles,
  seguir vendiendo a un hostil) viven en `app/prompt.py` — cualquier cambio
  de prompt re-corre una verificación de comportamiento end-to-end.
- **`ALLOWED_WA_IDS`**: con valor, solo se responde a esas identidades. No la
  vacíes sin decisión explícita del dueño de la instancia.
- **Multi-organización** (`VOCERO_MODE=cloud` + `CRM_ORGANIZATION` vacía): una
  Nea atiende a varios negocios. Tres reglas que no se negocian:
  1. **La conversación es de (organización, identidad)**, nunca de la
     identidad sola. La misma persona puede escribirle a dos negocios, y con
     la clave global el historial de uno entraba en el prompt del otro.
  2. **Quien piensa es el CRM**, no Nea. El cliente del modelo apunta a
     `/api/brains/llm` con la credencial derivada, y el CRM le pone la llave
     del miembro al reenviar. Si el CRM no ofrece pensar, NO se piensa — ni en
     el turno ni en el seguimiento: caer a `LLM_API_KEY` del entorno le
     cobraría al dueño de la plataforma el consumo de todos sus miembros.
  2b. **Sin modelo que OIGA no se transcribe.** Caer al que conversa devuelve
     una alucinación con pinta de transcripción, y quien la lee no tiene cómo
     saber que es falsa.
  3. **`ctx.crm` y `ctx.llm` se arman por turno.** El del arranque es un
     centinela que revienta al usarse: un camino que se olvide de armarlos
     tiene que fallar, no escribirle al negocio equivocado.
- **La credencial derivada es un contrato con el otro repositorio.** La cadena
  `vocero:cerebro:v1` y el base64url sin relleno están fijados en pruebas a
  ambos lados; desviarse un byte da 401 mudo en todo.
- **Degradación silenciosa**: LLM/CRM fallando jamás rompe el webhook ni manda
  texto roto; tras reintentos → silencio + handoff `error`.
- **La agenda la manda el CRM.** Vocero registra la oferta contra la
  conversación (por eso `conversationId` va SIEMPRE en `get_availability`) y
  decide qué es reservable. `offered_slots` de Nea es un ESPEJO: sirve para
  etiquetar con el día en palabras y frenar alucinaciones antes del viaje de
  red. Si el CRM responde `slot_not_offered`, su lista gana y el espejo se
  resincroniza — no se discute.
- **La agenda puede no existir.** En Vocero va detrás de la bandera `AGENDA`,
  apagada por defecto: esos endpoints responden 404. Se sondea al arrancar
  (`crm.agenda_available()`); sin agenda no se le enseñan al modelo las
  herramientas de agendar y el prompt se lo dice.

## Definición de Hecho

Typecheck + pytest verdes son el piso. "Hecho" = self-test de comportamiento
end-to-end: conversación real multi-turno, camino feliz e infeliz, iterando
hasta verde. Prohibido delegar la prueba al dueño.

## Credenciales

Nuevas variables → `.env.example` con placeholder `REEMPLAZA_...` y guía
inline. Jamás secretos en el repo ni en logs.

## Trabajo autónomo en Codex

Este espacio es la copia de trabajo Astra. El usuario autorizó implementar multitenancy, cobros Stripe y desplegar cambios en Vocero Cloud/dev. Resolver decisiones reversibles con el contexto disponible y completar pruebas y despliegue sin reconfirmaciones. No modificar producción ni otros proyectos. Mantener secretos fuera de chat, argv y Git. Los merges a main los realiza Kevin; se puede desplegar una rama de trabajo en dev. Leer specs y bitácora para retomar. Las instrucciones actuales del usuario prevalecen sobre procedimientos heredados.

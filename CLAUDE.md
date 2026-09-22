# Nea — Guía para Claude

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
| Lo que el lead ve en WhatsApp (Markdown → WhatsApp) | `app/formato.py` |
| El candado de cierre (conversación sin rumbo) | `app/stall.py` + gate 1.5 de `app/turn.py` |
| `/health` (versión, commit, modo, relay) | `app/main.py` · `app/version.py` |
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
- **Un turno sin CRM se reintenta, y solo ese.** Si el gate 2 no alcanza al
  CRM (red, 5xx, o un 404 con el mensaje todavía en el relay), `TurnoSinCrm`
  reprograma la MISMA ráfaga (`TURN_RETRY_DELAYS`): una por conversación, y
  lo que el lead escriba mientras tanto se fusiona en ella. Agotadas las
  esperas, handoff `error` en cuanto el CRM conteste. Nada posterior a leer el
  contexto se reintenta así: repetir un turno que ya pensó o escribió es
  contestarle dos veces al lead.
- **Todo lo que sale a WhatsApp pasa por `app/formato.py`** (turno y
  seguimiento), y el historial guarda lo convertido: WhatsApp no pinta
  Markdown, y si el modelo se ve escribiéndolo lo sigue escribiendo. Las URL y
  los correos no se tocan nunca.
- **El candado de cierre no es para siempre.** Tras la despedida, el relleno
  (`es_relleno`) se contesta con silencio durante `STALL_COOLDOWN_HOURS`; un
  mensaje con contenido reabre en el acto, la fase vuelve a descubrimiento y
  los contadores cuentan desde `stall_since_message_id`. El cierre se anota en
  la ficha del CRM (`cierre_sin_rumbo`) y se borra al reabrir.
- **La identidad va en la forma del CRM**: el teléfono canónico o
  `bsuid:<id>` (`identidad_del_mensaje`, espejo de `resolveIdentity` del CRM).
  Con el BSUID pelón, `/api/bot/context` da 404 y el lead nunca recibe
  respuesta.
- **`/health` da 200 mientras la base conteste.** La cola del relay es
  información para el CRM, no motivo para que Docker reinicie el contenedor. Y
  no lleva secretos ni URLs: el webhook del CRM trae su token en la ruta.
- **La agenda la manda el CRM.** Vocero registra la oferta contra la
  conversación (por eso `conversationId` va SIEMPRE en `get_availability`) y
  decide qué es reservable. `offered_slots` de Nea es un ESPEJO: sirve para
  etiquetar con el día en palabras y frenar alucinaciones antes del viaje de
  red. Si el CRM responde `slot_not_offered`, su lista gana y el espejo se
  resincroniza — no se discute.
- **La agenda puede no existir.** En Vocero va detrás de la bandera `AGENDA`,
  apagada por defecto: esos endpoints responden 404 **vacío** (un 404 con el
  sobre de error del CRM es otra cosa: no existe la conversación o la cita).
  Se sondea al arrancar y la respuesta caduca cada `AGENDA_PROBE_TTL_SECONDS`
  (`app/agenda.py`): encender o apagar la bandera en el CRM llega sin
  reiniciar Nea. Sin agenda no se le enseñan al modelo las herramientas de
  agendar y el prompt se lo dice.
- **No se ofrece lo que no hay con qué hacer.** Los recordatorios de la cita
  existen solo con la agenda v2 de Vocero Cloud (`supports_agenda_v2`): sin
  ella, `book_session` no lleva `recordatorios_aceptados` y el prompt dice que
  no hay recordatorios. Un campo obligatorio en una herramienta es una
  pregunta que el modelo le va a hacer al lead.

## Definición de Hecho

Typecheck + pytest verdes son el piso. "Hecho" = self-test de comportamiento
end-to-end: conversación real multi-turno, camino feliz e infeliz, iterando
hasta verde. Prohibido delegar la prueba al dueño.

## Credenciales

Nuevas variables → `.env.example` con placeholder `REEMPLAZA_...` y guía
inline. Jamás secretos en el repo ni en logs.

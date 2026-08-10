# Nea

**El agente de IA de agendamiento para WhatsApp, open source y self-hosted.**

Nea es un microservicio (FastAPI + Postgres) que atiende el WhatsApp de tu
negocio: conversa con cada lead como un humano bien entrenado, lo califica
según TUS criterios, y agenda citas reales en tu calendario — o lo despide con
dignidad cuando no es fit. Funciona en pareja con
[Vocero CRM](https://github.com/adriaavila/vocero-crm): el CRM es la fuente de
verdad (contactos, bandeja, pipeline, calendario, envío a Meta) y Nea es el
cerebro conversacional.

## Qué hace

- **Conversa de verdad**: una pregunta por mensaje, espeja el registro del
  lead, coalesce de ráfagas (varios mensajitos = UNA respuesta), señal de
  "escribiendo…", seguimiento único si el lead se queda callado.
- **Agenda con validación server-side**: propone horarios reales del
  calendario del CRM (máx. 3) y solo puede reservar un horario que él mismo
  ofreció — el LLM no puede inventar citas.
- **Multimedia**: transcribe notas de voz (Whisper), ve imágenes, extrae texto
  de documentos, entiende ubicaciones y stickers. Lo que no puede abrir, lo
  dice con honestidad.
- **Sabe escalar**: pide humano → handoff a la primera; 3 mensajes hostiles
  seguidos → cierre digno + alerta interna (conteo determinista, no depende
  del humor del LLM); duda fuera del conocimiento aprobado → handoff, no
  inventa.
- **Degradación silenciosa**: si el LLM o el CRM fallan, el lead jamás recibe
  texto roto — silencio, reintentos con backoff, colas persistentes
  (`relay`, `pending_send`) y handoff de error.

## La persona es del negocio, no del código

El **chasis conductual** (transparencia de IA, estilo WhatsApp, protocolo de
herramientas, reglas de hostilidad y escalado, los NUNCA duros) vive en
`app/prompt.py` y es genérico. **Todo lo que identifica a tu negocio** viene
de un `BusinessProfile` que se resuelve en este orden (`app/profile.py`):

1. **`GET /api/bot/profile` del CRM** — el agent profile + knowledge base que
   editas en la UI de Vocero (nombre del agente, tono, instrucciones, reglas
   de escalado, saludo, P/R aprobadas). Cache con TTL de 5 min: los cambios
   llegan sin reiniciar el bot.
2. **Brief local** — un markdown libre apuntado por `BRIEF_PATH` (ver
   `examples/brief.example.md`), para correr sin CRM con perfil o en dev.
3. **Perfil mínimo** — el agente se presenta y agenda, pero escala cualquier
   pregunta de fondo (y lo avisa en logs).

## Arquitectura

```
Meta Cloud API ── webhook ──► Nea (este repo)
                               │  1. verifica firma, dedup, encola
                               │  2. relay del payload CRUDO ──► Vocero CRM (webhook)
                               │  3. coalesce → contexto del CRM → LLM + tools
                               └─ envía SIEMPRE vía POST {CRM}/api/bot/messages
                                  (Nea jamás llama a graph.facebook.com para enviar)
```

Herramientas del LLM: `update_ficha` (calificación), `propose_slots` /
`book_session` (agenda), `route_out` (no califica; comparte los recursos
alternativos del perfil), `handoff` (pausa la IA en el CRM).

## Quickstart

Requisitos: Python 3.11+, Postgres propio (no el del CRM), una instancia de
Vocero CRM con el bot gateway habilitado (`BOT_API_KEY`), y una app de Meta
con WhatsApp Cloud API apuntando su webhook a este servicio.

```bash
git clone https://github.com/adriaavila/nea-agent && cd nea-agent
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                            # llena los REEMPLAZA_...
uvicorn app.main:app --port 8000                # migraciones corren al arranque
```

Salud: `GET /health`. El webhook de Meta va a `GET|POST /webhook` con tu
`VERIFY_TOKEN`.

### Docker / Coolify

El `Dockerfile` está listo para producción (healthcheck incluido). En Coolify:
app desde este repo + un Postgres, variables del `.env.example` en el runtime,
y el dominio del webhook hacia el puerto 8000.

## Piloto Allok: dónde cargar cada dato

Este despliegue está aislado de Allok y ya tiene la infraestructura creada:

| Componente | Dirección |
|---|---|
| Vocero CRM | `https://crm.allok.fun` |
| NEA | `https://agent.allok.fun` |
| Webhook de Meta | `https://agent.allok.fun/webhook` |
| WAHA tester | `https://waha-demo.frontia.app` · sesión `demo-nea-pilot` |

No copies secretos de Allok ni los guardes en Git. Los valores de conexión
entre contenedores, bases de datos, `BOT_API_KEY`, cifrado y verify tokens ya
están creados en Coolify; no hace falta regenerarlos para activar el piloto.

### 1. Coolify → proyecto `Vocero` → recurso `nea-agent`

Completa o reemplaza estas variables y vuelve a desplegar **solo NEA**:

| Variable | Dato que debes cargar |
|---|---|
| `OPENAI_API_KEY` | Key dedicada de OpenAI; reemplaza `pilot-not-configured` |
| `META_APP_SECRET` | App Secret de la app Meta dedicada |
| `ALLOWED_WA_IDS` | Solo el WhatsApp tester, con código de país y sin `+`; reemplaza `pilot-not-configured` |

`CRM_BASE_URL`, `CRM_WEBHOOK_URL`, `CRM_BOT_API_KEY`, `DATABASE_URL` y
`VERIFY_TOKEN` ya están configuradas. Copia el valor existente de
`VERIFY_TOKEN`: lo necesitarás en Meta, pero no lo cambies salvo que también
actualices el webhook allí.

### 2. Vocero CRM

1. Crea la primera cuenta en `https://crm.allok.fun/register`.
2. En **Settings → WhatsApp**, carga el WABA ID, Phone Number ID y access token
   permanente del número Meta dedicado.
3. Completa el perfil del agente y su knowledge base; NEA los leerá por
   `GET /api/bot/profile`.

Las credenciales de envío de Meta se cargan en la UI de Vocero, no en NEA.
Vocero es el único servicio que envía las respuestas por Meta Cloud API.

### 3. Meta Developers

En la app y número dedicados al piloto:

1. Configura el callback `https://agent.allok.fun/webhook`.
2. Usa como verify token el valor `VERIFY_TOKEN` del recurso `nea-agent`.
3. Suscribe el campo de mensajes de WhatsApp.
4. Copia **Settings → Basic → App Secret** a `META_APP_SECRET` de NEA.

No apuntes el número/app de Allok a este webhook.

### 4. Google Calendar → recurso `vocero-crm`

Comparte el calendario con la cuenta de servicio como writer y carga en
Coolify:

| Variable | Dato que debes cargar |
|---|---|
| `GOOGLE_CALENDAR_ID` | ID del calendario compartido |
| `GOOGLE_SERVICE_ACCOUNT_JSON_B64` | JSON completo de la cuenta de servicio, codificado en base64 |

Si esa cuenta no puede crear Google Meet, usa el fallback OAuth y añade
`GOOGLE_OAUTH_CLIENT_ID` y `GOOGLE_OAUTH_CLIENT_SECRET`; después conecta el
calendario desde **Vocero → Settings → Calendar**. OpenRouter es opcional y se
reserva al Lab de Vocero: NEA no lo necesita.

### 5. WAHA: solo en la máquina que ejecuta el self-test

WAHA simula a un usuario externo. Estas variables **no van en Coolify**:

```env
WAHA_BASE_URL=https://waha-demo.frontia.app
WAHA_SESSION=demo-nea-pilot
WAHA_API_KEY=<key scoped read+send guardada en Keychain>
LIVE_TARGET_NUMBER=<número Meta dedicado, con país y sin +>
```

Escanea la sesión `demo-nea-pilot`, confirma que quede `WORKING` y ejecuta
`python selftest/waha.py`. Mantén `ALLOWED_WA_IDS` limitado al número tester
durante todo el piloto.

### Orden de activación

1. WAHA en estado `WORKING`.
2. Cuenta inicial, WhatsApp y perfil configurados en Vocero.
3. OpenAI, App Secret y allowlist cargados en NEA.
4. Webhook verificado y suscrito en Meta.
5. Calendar configurado en Vocero.
6. Redeploy de `nea-agent` y `vocero-crm` y comprobación de
   `https://agent.allok.fun/health` y `https://crm.allok.fun/api/health`.
7. Conversación real desde WAHA; usa `/reset` antes de repetir un caso.

### Probar en seco

- **Allowlist de pruebas**: con `ALLOWED_WA_IDS` poblada, Nea solo responde a
  esas identidades (todo lo demás se releva al CRM sin respuesta). Vacíala
  únicamente para salir a producción.
- **Comando `/reset`**: desde una línea de la allowlist, reinicia la memoria
  de esa conversación (ficha limpia, IA reactivada) — cada prueba arranca con
  un lead virgen.
- `selftest/waha.py` es un harness opcional para mandar WhatsApp reales desde
  una línea tester vía [WAHA](https://waha.devlike.pro/). La key queda limitada
  a una sola sesión con `read + send`; conserva pausas, tope de mensajes,
  destino único y kill-switch de archivo.

## Definición de Hecho

Los tests unitarios (`pytest`, sin red ni Postgres) son el piso, no el techo.
"Hecho" = una conversación real multi-turno contra tu instancia, camino feliz
e infeliz (calificación, agenda, hostilidad, handoff), iterando hasta verde.
Los NUNCA del chasis en `app/prompt.py` no se relajan sin re-correr esa
verificación de comportamiento.

```bash
pytest -q          # 76 tests, todos offline
```

## Configuración

Todas las variables están documentadas en [`.env.example`](.env.example). Las
que definen la personalidad:

| Variable | Default | Qué hace |
|---|---|---|
| `AGENT_NAME` | `Nea` | Nombre del agente si el CRM no define uno |
| `AGENT_TIMEZONE` | `America/Caracas` | Zona horaria IANA para fechas del prompt |
| `BRIEF_PATH` | *(vacío)* | Markdown local con el brief del negocio (fallback) |

## Licencia

[MIT](LICENSE) — igual que Vocero. Úsalo, véndelo instalado, modifícalo.

# Nea

**El agente de IA de agendamiento para WhatsApp, open source y self-hosted.**

Nea es un microservicio (FastAPI + Postgres) que atiende el WhatsApp de tu
negocio: conversa con cada lead como un humano bien entrenado, lo califica
según TUS criterios, y agenda citas reales en tu calendario — o lo despide con
dignidad cuando no es fit. Funciona en pareja con
[Vocero CRM](https://github.com/kevinrivm/vocero-crm): el CRM es la fuente de
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
- **Sabe cuándo parar**: si la conversación no va a ningún lado (3 mensajes de
  relleno seguidos o 14 sin avanzar), se despide con una línea cálida y deja
  de perseguir. Al relleno de después («gracias», «ok 👍») le contesta con
  silencio; una pregunta de verdad reabre la conversación al momento. En el
  CRM, la ficha del contacto enseña «Cierre sin rumbo» mientras dura. Los
  umbrales se ajustan con `STALL_*`.
- **Escribe en WhatsApp, no en Markdown**: lo que el modelo escribe con
  `**negritas**`, títulos, tablas o `[enlaces](…)` se convierte antes de
  enviarse (`app/formato.py`), sin tocar las URL.
- **Degradación silenciosa**: si el LLM o el CRM fallan, el lead jamás recibe
  texto roto — silencio, reintentos con backoff, colas persistentes
  (`relay`, `pending_send`) y handoff de error.
- **Un CRM caído no se come el turno**: si al empezar un turno el CRM no
  contesta, la misma ráfaga se reintenta con esperas crecientes
  (`TURN_RETRY_DELAYS`, 15 s → 5 min, hasta ~8 min) y lo que el lead escriba
  mientras tanto se contesta en la MISMA respuesta. Si el CRM no vuelve a
  tiempo, la conversación pasa a un humano en cuanto conteste.

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

En el modo estándar, el de siempre:

```
Meta Cloud API ── webhook ──► Nea (este repo)
                               │  1. verifica firma, dedup, encola
                               │  2. relay del payload CRUDO ──► Vocero CRM (webhook)
                               │  3. coalesce → contexto del CRM → LLM + tools
                               └─ envía SIEMPRE vía POST {CRM}/api/bot/messages
                                  (Nea jamás llama a graph.facebook.com para enviar)
```

**El relay es lo que hace que el CRM vea el mensaje.** Cada POST de Meta se
encola en `relay_queue` (el Postgres de Nea) antes de parsear nada, y el
`RelayWorker` se lo reenvía crudo al webhook del CRM —firma intacta,
reintentos hasta 24 h con una espera que se dobla hasta
`RELAY_BACKOFF_CAP_SECONDS` (60 s): al volver el CRM, la cola se vacía en un
minuto como mucho—. Si esa cola no sale, el CRM se queda sin los entrantes y sin los
estados de entrega, y a un contacto nuevo Nea no le contesta:
`/api/bot/context` responde 404 hasta que el relay aterriza. Como solo corre
contra Postgres, la cubren las pruebas de `tests/test_pg_store.py` (ver
Definición de Hecho).

Herramientas del LLM: `update_ficha` (calificación), `propose_slots` /
`book_session` / `reschedule_session` (agenda, solo si el CRM agenda),
`route_out` (no califica; comparte los recursos alternativos del perfil),
`handoff` (pausa la IA en el CRM). En modo cloud, con la agenda v2 de Vocero
Cloud, también `list_bookings` y `cancel_session`.

### Modo cloud (opcional): detrás de un Vocero multitenant

Con `VOCERO_MODE=cloud` el camino se invierte — el CRM recibe el mensaje y se
lo despacha a Nea:

```
Meta ──► Vocero CRM (multitenant)
             │  el mensaje YA está en la bandeja
             ▼  POST /vocero/dispatch (firmado con el secreto de la org)
           Nea
             └─ contesta por POST {CRM}/api/brains/* con ese mismo secreto
```

**La misma Nea sirve a cualquier negocio sin tocarle una línea**: nombre, tono,
instrucciones y conocimiento salen del contexto de la conversación, no del
código ni del brief local. Cambiar el conocimiento en el CRM cambia lo que Nea
responde, sin redesplegar nada.

#### Una instancia por negocio, o una para todos

Con `CRM_ORGANIZATION` puesta, esta Nea sirve a ESE negocio: su secreto y su
clave de LLM son los de él.

**Déjala vacía y sirve a todos los que el CRM le suscriba.** Entonces:

- El secreto es del **despliegue**, no de una organización. Con él se verifica
  todo despacho, y de él se **deriva** la credencial de cada negocio:
  `HMAC-SHA256(secreto, "vocero:cerebro:v1:{organizationId}")` en base64url.
  Nea la calcula; el CRM no se la manda.
- **Cada negocio paga su propio consumo, y su clave no llega hasta aquí.**
  Quien piensa es el CRM: Nea le pide por `/api/brains/llm` —un endpoint
  compatible con OpenAI— y él le pone la credencial de esa organización al
  reenviar al proveedor. Una llave que no viaja no se filtra, y cuando el
  proveedor la rechaza el 401 lo recibe quien puede avisarle a su dueño.
  Si el CRM no ofrece pensar, Nea se calla y deja la conversación a un humano.
- **Las conversaciones no se cruzan.** La clave es (organización, identidad):
  la misma persona puede escribirle a dos negocios sin que el historial de uno
  aparezca en el prompt del otro.

El despliegue lo registra el dueño de la plataforma desde el CRM
(`pnpm cerebro:registrar`), y cada miembro lo elige en Ajustes → Cerebro: sin
URL que pegar ni secreto que copiar, porque el secreto no es suyo.

Sin la bandera no cambia nada: el webhook de Meta, el relay y `/api/bot/*`
siguen siendo los de siempre. Las variables del modo cloud están en
`.env.example`.

## Instalar: Nea delante de Vocero raíz

Así se instala en el modo estándar, el de siempre (sin `VOCERO_MODE`): Meta le
manda los webhooks a Nea, Nea le releva cada uno al CRM y conversa por
`/api/bot/*`. Nea no tiene el token de WhatsApp ni le escribe a Meta: **todo lo
que el lead recibe sale por `POST {CRM}/api/bot/messages`**, y es el CRM quien
lo envía y lo deja en la bandeja.

Qué cambia en cada versión y cómo actualizar una Nea que ya corre:
[`CHANGELOG.md`](CHANGELOG.md).

### 1. Lo que pone el CRM

En las variables de Vocero raíz:

- `BOT_API_KEY` (`openssl rand -hex 32`) abre `/api/bot/*`. La misma va en
  Nea como `CRM_BOT_API_KEY`.
- `META_WEBHOOK_VERIFY_TOKEN` es el token del webhook del CRM. Va al final de
  `CRM_WEBHOOK_URL`.
- `META_APP_SECRET`, el App Secret de tu app de Meta, igual que en Nea. Nea
  verifica la firma de Meta y le releva al CRM el payload con la firma
  intacta, así que el CRM también la verifica.
- `AGENDA=on` si quieres que Nea agende: viene apagada. Nea lo detecta al
  arrancar y lo vuelve a preguntar cada minuto (`AGENDA_PROBE_TTL_SECONDS`),
  así que encenderla no exige reiniciarla. Contra un CRM sin agenda no ofrece
  horarios ni promete citas: califica y escala a un humano. La agenda la lleva
  el CRM: Nea le pide los huecos, él registra lo ofrecido y solo acepta
  reservar uno de esos.

En Coolify, una variable nueva entra con un redeploy, no con un restart.

Y en la app del CRM:

- El número de WhatsApp conectado (Configuración → WhatsApp): es el CRM quien
  envía.
- **El agente incluido de Vocero, apagado** (pestaña Agente), o el CRM sin
  `OPENROUTER_API_TOKEN`. Si contestan los dos, el cliente recibe dos
  respuestas a cada mensaje.

### 2. Nea desde la imagen

Cada tag `vX.Y.Z` de este repo publica `ghcr.io/kevinrivm/nea-agent:X.Y.Z`
(además de `X.Y` y `latest`) para `linux/amd64`, con la versión y el commit
horneados para `/health` (`.github/workflows/imagen.yml`). Fija la etiqueta
exacta, nunca `latest`:

```bash
docker pull ghcr.io/kevinrivm/nea-agent:1.0.0
```

La imagen aplica las migraciones al arrancar (`migrations/*.sql`: son
idempotentes y corren todas en cada arranque) y trae su HEALTHCHECK:
`GET /health` en el puerto 8000, cada 30 s.

En Coolify, dentro del proyecto del CRM:

1. Un **PostgreSQL** para Nea, aparte del del CRM (el CI prueba contra el 16).
2. **+ New → Docker Image**: `ghcr.io/kevinrivm/nea-agent`, etiqueta `1.0.0`,
   puerto `8000` y un dominio con https: Meta tiene que alcanzarlo.
3. Las variables mínimas de abajo, y deploy.

No pongas `NEA_VERSION` en las variables: pisaría la de la imagen. Deja `PORT`
en 8000: el HEALTHCHECK siempre pregunta a ese puerto. Y no dejes dos Nea
contra la misma base: cada una reenviaría por su cuenta lo pendiente.

¿Un fork u otra arquitectura? Construye tu imagen con los mismos build args;
sin ellos, `/health` dice `"version": "dev"`:

```bash
docker build --build-arg NEA_VERSION=1.0.0 \
  --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) -t nea-agent:1.0.0 .
```

O deja que la construya GitHub: con Actions encendido en tu fork, un tag
`vX.Y.Z` publica `ghcr.io/<tu-usuario>/nea-agent:X.Y.Z`. GitHub crea ese
paquete como privado: hazlo público (Package settings → Change visibility) o
dale a tu servidor credenciales del registro, o Coolify no podrá descargarlo.

### 3. Variables mínimas

| Variable | Qué va | Si falta |
|---|---|---|
| `DATABASE_URL` | El Postgres de Nea: `postgresql://usuario:clave@host:5432/nea` | Nea no arranca |
| `VERIFY_TOKEN` | Un token que inventas tú. El mismo va en el override del webhook | Meta no puede verificar el webhook: `GET /webhook` da 403 |
| `META_APP_SECRET` | El App Secret de tu app de Meta, el mismo del CRM | No se verifica la firma: cualquiera puede postear al webhook. Solo para desarrollo |
| `CRM_BASE_URL` | `https://crm.tu-negocio.com`, sin `/` al final | Apunta a `http://localhost:3000` |
| `CRM_WEBHOOK_URL` | `https://crm.tu-negocio.com/api/webhooks/wa/<META_WEBHOOK_VERIFY_TOKEN del CRM>` | El relay no entrega nada: el CRM no ve los mensajes y a un contacto nuevo Nea no le contesta |
| `CRM_BOT_API_KEY` | El `BOT_API_KEY` del CRM | El CRM contesta 401 y Nea no le contesta a nadie |
| `LLM_API_KEY` | La llave del proveedor del modelo | Nea no arranca |
| `LLM_BASE_URL` | Con OpenRouter, `https://openrouter.ai/api/v1` | OpenAI |
| `LLM_MODEL` | Un modelo rápido que use herramientas. En OpenRouter lleva prefijo: `z-ai/glm-5.3-flash` | `gpt-4o-mini` |
| `LLM_TRANSCRIBE_MODEL` | El que oye las notas de voz. Fuera de OpenAI, uno que acepte audio: `google/gemini-2.5-flash` | `whisper-1`, que solo existe en OpenAI: con otro proveedor falla toda nota de voz |
| `ALLOWED_WA_IDS` | Mientras pruebas, tu número (`5215512345678`) | Nea le contesta a todos |

`VOCERO_MODE` se queda vacía: eso es el modo estándar. Lo demás (zona horaria,
candado de cierre, reintentos, tiempos) tiene default y está explicado en
[`.env.example`](.env.example). Los nombres viejos `OPENAI_*` siguen
funcionando.

### 4. El webhook de Meta, a Nea

Nea recibe a Meta en `https://nea.tu-dominio.com/webhook`: `GET` para la
verificación (con tu `VERIFY_TOKEN`) y `POST` para los eventos (con
`META_APP_SECRET` puesto, una firma inválida o ausente da 401).

Apúntalo con un **override a nivel del número de teléfono**. Meta busca a
dónde mandar cada webhook en este orden: el override del número, el de la WABA
y, al final, la URL de callback de la app. El del número gana, y nada del CRM
lo toca.

```http
POST https://graph.facebook.com/v25.0/{PHONE_NUMBER_ID}
Authorization: Bearer {TOKEN_DE_WHATSAPP}
Content-Type: application/json

{"webhook_configuration": {"override_callback_uri": "https://nea.tu-dominio.com/webhook",
                           "verify_token": "{VERIFY_TOKEN de Nea}"}}
```

Antes de mandarlo:

- Nea arriba y contestando `GET /webhook` con ese mismo token: Meta verifica
  la URL.
- La app suscrita a la WABA (`GET /{WABA_ID}/subscribed_apps` no viene vacío)
  y el campo `messages` suscrito en la app de Meta (App Dashboard → Webhooks).
  El override cambia a dónde llegan los webhooks, no hace que existan.
- El token con el permiso `whatsapp_business_management`, y la URL de 200
  caracteres o menos.

Que el POST conteste bien no basta. Reléelo:

```http
GET https://graph.facebook.com/v25.0/{PHONE_NUMBER_ID}?fields=webhook_configuration
```

`webhook_configuration.phone_number` tiene que ser la URL de Nea;
`whatsapp_business_account` y `application` son los de respaldo. Para quitarlo,
el mismo POST con `"override_callback_uri": ""`: los webhooks vuelven al
override de la WABA (en una instalación de Vocero, el CRM) o, si no hay, al
callback de la app. Las plantillas no siguen ningún override: sus eventos van
siempre al callback de la app.

**La trampa: `POST /{WABA_ID}/subscribed_apps` sin cuerpo.** Así documenta Meta
cómo se *borra* el override de la WABA. Vocero raíz hasta 1.3.0 hace esa
llamada cada vez que guardas la conexión en Configuración → WhatsApp (también
al cambiar el token): si Nea estaba en la WABA, deja de recibir sin ningún
aviso. Desde 1.4.0 el CRM primero consulta y respeta un override que ya exista,
pero si esa consulta falla, re-suscribe igual. El override del número no se
toca con esa llamada: por eso Nea va ahí.

### 5. Comprobar

- `GET https://nea.tu-dominio.com/health` responde 200 con
  `"version": "1.0.0"`, `"mode": "estándar"` y `relay.pendientes` en 0
  (detalle en [`/health`](#health)).
- Escríbele al número desde uno de `ALLOWED_WA_IDS`: tu mensaje aparece en la
  bandeja del CRM y la respuesta de Nea sale desde ahí.
- Para atender a todos, vacía `ALLOWED_WA_IDS` y redespliega. Es decisión del
  dueño del negocio.

## `/health`

`GET /health` responde 200 mientras la base conteste y 503 si no. Solo la base
decide el código: es lo que mira el HEALTHCHECK de la imagen, y una cola
atrasada no se arregla reiniciando el contenedor. No lleva secretos ni URLs.

```json
{"status": "ok", "db": "ok", "version": "1.0.0", "commit": "a1b2c3d",
 "commitVerified": true, "mode": "estándar",
 "relay": {"pendientes": 0, "masViejoSegundos": null, "ultimoErrorEn": null}}
```

| Campo | Qué dice |
|---|---|
| `status` | `ok`, o `degraded` con 503 si la base no contesta |
| `db` | `ok` o `error` |
| `version` | La versión horneada en la imagen (`NEA_VERSION`). `dev` si se construyó sin ella |
| `commit` | Los primeros 7 caracteres del commit. Solo aparece si se conoce |
| `commitVerified` | `true` si el commit salió del build; `false` si es el `SOURCE_COMMIT` que la plataforma puso en el entorno al arrancar, que puede estar desfasado |
| `mode` | `estándar`, `cloud` o `multiorg`, con acento |
| `relay` | Solo en modo estándar: en cloud el CRM ya tiene el mensaje. `null` si no se pudo leer la cola, y el código sigue en 200 |
| `relay.pendientes` | Webhooks de Meta que todavía no llegan al CRM |
| `relay.masViejoSegundos` | Cuántos segundos lleva esperando el más viejo. `null` si no hay pendientes |
| `relay.ultimoErrorEn` | La última vez que falló una entrega al CRM (ISO 8601, UTC). No se borra cuando la cola se vacía |

Si `pendientes` no baja y `ultimoErrorEn` es reciente, el relay no llega al
CRM: revisa `CRM_WEBHOOK_URL` (dominio y token), que el CRM esté arriba y que
su `META_APP_SECRET` sea el de tu app. Cada webhook se reintenta hasta 24 h;
después se abandona y deja de contar.

Vocero raíz 1.4.0 lee este `/health` para su tarjeta «Quién responde a tus
clientes» (pantalla Agente) si el CRM tiene `BRAIN_HEALTH_URL` con la
dirección interna de Nea, p. ej. `http://nea:8000/health` con el alias de red
de Coolify. Ahí se ve «Nea · en línea · v1.0.0 · modo estándar · 0 mensajes
por relevar», y un aviso rojo si el agente incluido del CRM también contesta.

## Desarrollo local

Requisitos: Python 3.11+, un Postgres propio y un Vocero CRM con lo del paso 1.

```bash
git clone https://github.com/kevinrivm/nea-agent && cd nea-agent
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                            # llena los REEMPLAZA_...
uvicorn app.main:app --port 8000                # migraciones corren al arranque
```

El webhook de Meta va a `GET|POST /webhook` con tu `VERIFY_TOKEN`.

### Probar en seco

- **Allowlist de pruebas**: con `ALLOWED_WA_IDS` poblada, Nea solo responde a
  esas identidades (todo lo demás se releva al CRM sin respuesta). Vacíala
  únicamente para salir a producción. Una identidad es el teléfono con lada
  o, para quien escribe sin compartir su número, el BSUID como lo enseña el
  CRM (`bsuid:US.1349…`; sin el prefijo también vale).
- **Comando `/reset`**: desde una línea listada en `TESTER_WA_IDS`, reinicia
  la memoria de esa conversación (ficha limpia, IA reactivada) — cada prueba
  arranca con un lead virgen. Es una variable aparte de `ALLOWED_WA_IDS` a
  propósito: en producción la allowlist va vacía para atender a todos los
  leads, y si el comando colgara de ella no habría forma de resetear sin
  dejar de atenderlos.
- `selftest/evolution.py` es un harness opcional para mandar WhatsApp reales
  desde una línea tester vía [Evolution API](https://doc.evolution-api.com/),
  con pausas mínimas, tope de mensajes y kill-switch de archivo.

### Prueba de punta a punta contra Vocero raíz

`scripts/e2e_contra_raiz.py` monta el par como se instala en modo estándar
(Meta → Nea → relay al CRM; Nea contesta por `/api/bot/*`) y hace de Meta y
de cliente: manda webhooks con la forma real de la Cloud API, firmados con
`META_APP_SECRET`, y comprueba lo observable en la API del CRM, su base y el
outbox del wa-mock. Nada sale a Meta ni a WhatsApp: el CRM envía por su
wa-mock.

**Requisitos**: un checkout de [vocero-crm](https://github.com/kevinrivm/vocero-crm)
con `pnpm install` hecho (el guion lo arranca con `next dev` y le aplica las
migraciones), Node en el `PATH`, este repo con sus dependencias, y un Postgres
con dos bases (una para el CRM y otra para Nea; vacías o de una corrida
anterior, las dos sirven). El guion levanta y apaga el CRM y Nea; el Postgres
no.

**Variables** (del entorno o de uno o más `--env-file`; de esos archivos solo
se leen estas tres, así que puede ser el vault de operación):

| Variable | Qué es |
|---|---|
| `LLM_API_KEY` | Llave de OpenRouter. Nunca va por argv ni se imprime |
| `CRM_DATABASE_URL` | Base del CRM |
| `NEA_DATABASE_URL` | Base de Nea |

Lo demás (`BOT_API_KEY`, `META_APP_SECRET`, tokens del webhook, secretos de
sesión y de cifrado) lo inventa cada corrida.

```bash
python scripts/e2e_contra_raiz.py --crm-dir ../vocero-crm \
  --env-file ../.env --env-file ./runtime-e2e.env --out ./e2e-salida
# --escenarios 1-5,9 para correr solo algunos · --presupuesto 0.50 (USD)
```

Los puertos son opciones (`--crm-port 3800`, `--nea-port 8100`,
`--medidor-port 8190`, los de por defecto): para correr dos pares a la vez, cada
uno con sus puertos, su `--out` y su propio Postgres. El `--crm-dir` tampoco se
comparte entre corridas simultáneas: `next dev` escribe su `.next` ahí.

**Qué comprueba**, con un cliente nuevo por historia: (1) el primer mensaje
llega a la bandeja del CRM en segundos y la respuesta de Nea sale por el
wa-mock sin Markdown; (2) `delivered` y `read` de Meta avanzan el estado del
mensaje en el CRM; (3) pedir horario trae huecos que el CRM registró como
ofrecidos, elegir uno crea la cita, el lead sube a la siguiente etapa abierta
y la confirmación dice «Enlace de la reunión» con la sala fija (no «Zoom»), y
Nea no afirma que un día «solo tiene mañana» por lo que no vio;
(4) Nea sabe a qué hora quedó la cita (bloque `booking` de `/api/bot/context`)
y no ofrece recordatorios, que el CRM raíz no manda;
(5) pedir una persona deja el handoff en la conversación y una despedida; (6)
tres rellenos seguidos cierran con una despedida y `cierre_sin_rumbo` en la
ficha, el relleno siguiente se calla y una pregunta con contenido reabre; (7)
con el CRM apagado ~20 s y dos mensajes del cliente en medio, el relay entrega
los dos al volver y el cliente recibe UNA respuesta que contesta los dos (un
solo turno con la ráfaga completa, sin handoff); (8) volver a guardar la conexión de WhatsApp
no borra el override de la WABA que fija Nea; (9) `/health` enseña versión,
modo `estándar` y la cola del relay en 0.

En `--out` quedan una transcripción por cliente, `resultado.json` (evidencia
por escenario, latencia por turno con mediana y p95, gasto del modelo),
`medidor-llm.json` y los logs del CRM y de Nea. Sale con 0 si todo pasa, 1 si
algún escenario falla y 2 si no se pudo montar el par.

**Cuesta unos centavos de LLM** (~20 turnos contra `z-ai/glm-5.3-flash`) y
tarda unos 5 min; el escenario 7 es el más largo: apaga y vuelve a levantar el
CRM y espera a que el turno que no lo alcanzó se reintente. Nea
habla con OpenRouter a través de un medidor local que reenvía los bytes tal
cual, cuenta los tokens y, antes de pasarse del `--presupuesto`, contesta 402
sin llamar; tampoco deja pasar otro modelo que el de la prueba.

## Definición de Hecho

Los tests unitarios (`pytest`, sin red ni Postgres) son el piso, no el techo.
"Hecho" = una conversación real multi-turno contra tu instancia, camino feliz
e infeliz (calificación, agenda, hostilidad, handoff), iterando hasta verde.
Los NUNCA del chasis en `app/prompt.py` no se relajan sin re-correr esa
verificación de comportamiento.

```bash
pip install -r requirements-dev.txt   # pytest, respx y compañía, con versión exacta
pytest -q          # 496 tests: 457 offline + 39 de PgStore, que se saltan sin Postgres
```

Las de `tests/test_pg_store.py` corren `PgStore` contra un Postgres de verdad
(`MemoryStore` no tiene SQL ni filas que mapear, y ahí es donde se rompió el
relay). Apúntalas a un servidor desechable donde el usuario pueda crear bases:
cada corrida crea la suya, le aplica las migraciones y la borra al terminar.
En CI corren en su propio job con `postgres:16`.

```bash
TEST_DATABASE_URL=postgresql://usuario:clave@localhost:5432/postgres pytest -q tests/test_pg_store.py
```

## Configuración

Todas las variables están documentadas en [`.env.example`](.env.example). Las
que definen la personalidad:

| Variable | Default | Qué hace |
|---|---|---|
| `AGENT_NAME` | `Nea` | Nombre del agente si el CRM no define uno |
| `AGENT_TIMEZONE` | `America/Mexico_City` | Zona horaria IANA para fechas del prompt |
| `BRIEF_PATH` | *(vacío)* | Markdown local con el brief del negocio (fallback) |
| `STALL_FILLER_STREAK` | `3` | Mensajes de relleno seguidos que cierran la conversación (0 = apagado) |
| `STALL_MAX_TURNS` | `14` | Mensajes del lead sin avanzar que la cierran (0 = apagado) |
| `STALL_COOLDOWN_HOURS` | `24` | Tras el cierre, cuánto se contesta el relleno con silencio |

## Licencia

[MIT](LICENSE) — igual que Vocero. Úsalo, véndelo instalado, modifícalo.

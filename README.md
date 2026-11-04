# Nea

**El agente de IA de agendamiento para WhatsApp, Instagram, Messenger y
TikTok, open source y self-hosted.**

Nea es un microservicio (FastAPI + Postgres) que atiende los mensajes de tu
negocio: conversa con cada lead como un humano bien entrenado, lo califica
según TUS criterios, y agenda citas reales en tu calendario — o lo despide con
dignidad cuando no es fit. Funciona en pareja con
[Vocero CRM](https://github.com/kevinrivm/vocero-crm): el CRM es la fuente de
verdad (contactos, bandeja, pipeline, calendario, envío a Meta) y Nea es el
cerebro conversacional.

## Qué hace

- **Conversa de verdad**: una pregunta por mensaje, espeja el registro del
  lead, una respuesta por ráfaga (el CRM junta los mensajitos antes de
  despachárselos), señal de "escribiendo…", seguimiento único si el lead se
  queda callado.
- **En todos los canales del CRM**: WhatsApp, Instagram, Messenger y TikTok le
  llegan igual, por el despacho del CRM, y el agente sabe por cuál le
  escriben.
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
  texto roto — silencio, reintentos con backoff, colas persistentes (el
  despacho aceptado y `pending_send`) y handoff de error.

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

Desde la 2.0, el CRM recibe todo y Nea solo piensa:

```
Meta (WhatsApp) ─┐
Zernio (Instagram, Messenger, TikTok) ─┴─ webhook ──► Vocero CRM
                                                        │  guarda el mensaje (ya está en la bandeja)
                                                        │  junta la ráfaga
                                                        ▼  POST /vocero/dispatch, firmado
                                                       Nea (este repo)
                                                        │  contexto del CRM → LLM + tools
                                                        └─ contesta SIEMPRE por {CRM}/api/bot/*
                                                           (Nea jamás le habla a Meta ni a Zernio)
```

**El despacho es la única entrada.** Nea responde 2xx en cuanto lo guarda en su
cola durable (`dispatch_inbox`, en su Postgres) y piensa después; el CRM
reintenta lo que no se le acusó y, si Nea no contesta, le pasa la conversación
a una persona. Un mismo mensaje puede venir en dos despachos (la ráfaga viaja
completa cada vez): Nea lo lee una sola vez. El contrato está en
`vocero-crm/specs/021-despacho-estandar/contracts/despacho.md`, y una prueba
gemela (`tests/test_despacho_raiz.py`) fija el cuerpo y la firma byte a byte.

Nea 1.x recibía el webhook de Meta y se lo reenviaba al CRM (el «relay»). Eso
dejaba a Instagram, Messenger y TikTok sin agente y obligaba a guardar el App
Secret de Meta en dos lugares: se fue en la 2.0. Cómo actualizar:
[`CHANGELOG.md`](CHANGELOG.md).

Herramientas del LLM: `update_ficha` (calificación), `propose_slots` /
`book_session` / `reschedule_session` (agenda, solo si el CRM agenda),
`route_out` (no califica; comparte los recursos alternativos del perfil),
`handoff` (pausa la IA en el CRM). En modo cloud, con la agenda v2 de Vocero
Cloud, también `list_bookings` y `cancel_session`.

### Modo cloud (opcional): detrás de un Vocero multitenant

Con `VOCERO_MODE=cloud` el despacho llega de un Vocero multitenant: viene
firmado con el secreto de la organización y Nea contesta por `/api/brains/*`:

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

#### Cuando el CRM elimina una conversación

El CRM avisa por la misma ruta y con la misma firma, con
`"type": "conversation.deleted"` en el cuerpo. Nea guarda el aviso, responde
200 y borra lo que recordaba de esa conversación: historial, fase, horarios
ofrecidos, envíos pendientes y los despachos guardados. Solo en la
organización del aviso.

Si el aviso no llega (Nea reiniciándose, un CRM que no avisa), hay una red:
cuando la misma persona vuelve con un `conversation.id` distinto del que Nea
recordaba, el turno empieza de cero. Esa red también cubre el modo estándar.

El despliegue lo registra el dueño de la plataforma desde el CRM
(`pnpm cerebro:registrar`), y cada miembro lo elige en Ajustes → Cerebro: sin
URL que pegar ni secreto que copiar, porque el secreto no es suyo.

Sin la bandera, Nea habla con Vocero raíz por `/api/bot/*`. Las variables
del modo cloud están en `.env.example`.

## Instalar: Nea junto a Vocero raíz

Así se instala en el modo estándar (sin `VOCERO_MODE`): el CRM recibe a Meta y
a Zernio, y le despacha cada ráfaga a Nea. Nea no tiene el token de WhatsApp
ni le escribe a Meta: **todo lo que el lead recibe sale por
`POST {CRM}/api/bot/messages`**, y es el CRM quien lo envía y lo deja en la
bandeja.

Qué cambia en cada versión y cómo actualizar una Nea que ya corre:
[`CHANGELOG.md`](CHANGELOG.md).

### 1. Lo que pone el CRM

En las variables de Vocero raíz:

- `BOT_API_KEY` (`openssl rand -hex 32`) abre `/api/bot/*` **y firma cada
  despacho**. La misma va en Nea como `CRM_BOT_API_KEY`.
- `BRAIN_DISPATCH_URL`: a dónde despachar, `https://nea.tu-dominio.com/vocero/dispatch`
  (o la dirección interna, `http://nea:8000/vocero/dispatch`, si los dos viven
  en la misma red de Coolify).
- `AGENDA=on` si quieres que Nea agende: viene apagada. Nea lo detecta al
  arrancar y lo vuelve a preguntar cada minuto (`AGENDA_PROBE_TTL_SECONDS`),
  así que encenderla no exige reiniciarla. Contra un CRM sin agenda no ofrece
  horarios ni promete citas: califica y escala a un humano. La agenda la lleva
  el CRM: Nea le pide los huecos, él registra lo ofrecido y solo acepta
  reservar uno de esos.

En Coolify, una variable nueva entra con un redeploy, no con un restart.

Y en la app del CRM:

- El número de WhatsApp conectado (Configuración → WhatsApp) y, si los usas,
  Instagram, Messenger o TikTok (Ajustes → Canales). El webhook de Meta es el
  del CRM: ya no se mueve a ningún lado.
- Con el despacho activo, el agente incluido de Vocero se calla solo: no hay
  doble respuesta.

### 2. Nea desde la imagen

Cada tag `vX.Y.Z` de este repo publica `ghcr.io/kevinrivm/nea-agent:X.Y.Z`
(además de `X.Y` y `latest`) para `linux/amd64`, con la versión y el commit
horneados para `/health` (`.github/workflows/imagen.yml`). Fija la etiqueta
exacta, nunca `latest`:

```bash
docker pull ghcr.io/kevinrivm/nea-agent:2.0.0
```

La imagen aplica las migraciones al arrancar (`migrations/*.sql`: son
idempotentes y corren todas en cada arranque) y trae su HEALTHCHECK:
`GET /health` en el puerto 8000, cada 30 s.

En Coolify, dentro del proyecto del CRM:

1. La base: **la misma del CRM** con `DATABASE_SCHEMA=nea` (Nea crea su
   esquema y no toca el del CRM), o un PostgreSQL aparte si lo prefieres (el CI
   prueba contra el 16).
2. **+ New → Docker Image**: `ghcr.io/kevinrivm/nea-agent`, etiqueta `2.0.0`,
   puerto `8000`. Con la dirección interna en `BRAIN_DISPATCH_URL` no necesita
   dominio público.
3. Las variables mínimas de abajo, y deploy.

No pongas `NEA_VERSION` en las variables: pisaría la de la imagen. Deja `PORT`
en 8000: el HEALTHCHECK siempre pregunta a ese puerto. La imagen corre sin
root (UID 10001): si montas archivos, como el `BRIEF_PATH`, que ese usuario
pueda leerlos.

¿Un fork u otra arquitectura? Construye tu imagen con los mismos build args;
sin ellos, `/health` dice `"version": "dev"`:

```bash
docker build --build-arg NEA_VERSION=2.0.0 \
  --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) -t nea-agent:2.0.0 .
```

O deja que la construya GitHub: con Actions encendido en tu fork, un tag
`vX.Y.Z` publica `ghcr.io/<tu-usuario>/nea-agent:X.Y.Z`. GitHub crea ese
paquete como privado: hazlo público (Package settings → Change visibility) o
dale a tu servidor credenciales del registro, o Coolify no podrá descargarlo.

### 3. Variables mínimas

| Variable | Qué va | Si falta |
|---|---|---|
| `DATABASE_URL` | El Postgres: el del CRM o uno propio | Nea no arranca |
| `DATABASE_SCHEMA` | `nea` si comparte la base del CRM | Usa `public`: con la base del CRM, sus tablas quedarían mezcladas |
| `CRM_BASE_URL` | `https://crm.tu-negocio.com` (o la dirección interna), sin `/` al final | Apunta a `http://localhost:3000` |
| `CRM_BOT_API_KEY` | El `BOT_API_KEY` del CRM | Todo despacho da 401 y el CRM le pasa cada conversación a una persona |
| `LLM_API_KEY` | Opcional. Vacía, Nea piensa por el CRM: la llave y el modelo se eligen en Ajustes → IA y Nea llama a `/api/bot/llm` con `CRM_BOT_API_KEY`. Con valor, cerebro propio con su proveedor | Piensa por el CRM; sin IA en el CRM, cada conversación pasa a una persona |
| `LLM_BASE_URL` | Con OpenRouter, `https://openrouter.ai/api/v1` | OpenAI |
| `LLM_MODEL` | Un modelo rápido que use herramientas. En OpenRouter lleva prefijo: `z-ai/glm-5.3-flash`. Pensando por el CRM manda el modelo de Ajustes → IA; este es el respaldo | `gpt-4o-mini` |
| `LLM_TRANSCRIBE_MODEL` | El que oye las notas de voz. Fuera de OpenAI, uno que acepte audio: `google/gemini-2.5-flash` | `whisper-1`, que solo existe en OpenAI. Con otro proveedor, o pensando por el CRM, Nea usa `google/gemini-3.5-flash-lite` |
| `ALLOWED_WA_IDS` | Mientras pruebas, tu número (`5215512345678`) o tu identidad de Instagram (`ig:…`) | Nea le contesta a todos |

`VOCERO_MODE` se queda vacía: eso es el modo estándar. Lo demás (zona horaria,
candado de cierre, tiempos) tiene default y está explicado en
[`.env.example`](.env.example). Los nombres viejos `OPENAI_*` siguen
funcionando.

### 4. Comprobar

- `GET https://nea.tu-dominio.com/health` (o la dirección interna) responde 200
  con `"version": "2.0.0"`, `"mode": "estándar"` y `webhookDeMeta.ultimoEn` en
  `null` (detalle en [`/health`](#health)).
- Escríbele al negocio desde uno de `ALLOWED_WA_IDS`: tu mensaje aparece en la
  bandeja del CRM y la respuesta de Nea sale desde ahí.
- Para atender a todos, vacía `ALLOWED_WA_IDS` y redespliega. Es decisión del
  dueño del negocio.

### Si vienes de Nea 1.x

Nea 1.x recibía el webhook de Meta. En la 2.0 ese webhook vuelve al CRM:

1. En el CRM, `BRAIN_DISPATCH_URL` hacia `/vocero/dispatch` de Nea, y redeploy.
2. **Quita el override del número** que apuntaba a Nea (el mismo `POST` con
   `"override_callback_uri": ""`): los webhooks vuelven al override de la WABA,
   que es el del CRM. Compruébalo con
   `GET /{PHONE_NUMBER_ID}?fields=webhook_configuration`.
3. Actualiza Nea a la 2.0. Puedes quitarle `META_APP_SECRET`, `VERIFY_TOKEN`,
   `CRM_WEBHOOK_URL`, `RELAY_BACKOFF_CAP_SECONDS`, `COALESCE_SECONDS`,
   `TYPING_DELAY_SECONDS` y `TURN_RETRY_DELAYS`: ya no se leen, y si siguen
   ahí Nea lo avisa al arrancar.

Si actualizas Nea antes de mover el webhook, no se pierde nada: Nea le contesta
503 a Meta para que reintente, lo deja en el log como error y en `/health`
(`webhookDeMeta.ultimoEn`). En cuanto el webhook apunta al CRM, los mensajes
siguen llegando.

## `/health`

`GET /health` responde 200 mientras la base conteste y 503 si no. Solo la base
decide el código: es lo que mira el HEALTHCHECK de la imagen. No lleva
secretos ni URLs.

```json
{"status": "ok", "db": "ok", "version": "2.0.0", "commit": "a1b2c3d",
 "commitVerified": true, "mode": "estándar", "llm": "crm",
 "webhookDeMeta": {"ultimoEn": null}}
```

| Campo | Qué dice |
|---|---|
| `status` | `ok`, o `degraded` con 503 si la base no contesta |
| `db` | `ok` o `error` |
| `version` | La versión horneada en la imagen (`NEA_VERSION`). `dev` si se construyó sin ella |
| `commit` | Los primeros 7 caracteres del commit. Solo aparece si se conoce |
| `commitVerified` | `true` si el commit salió del build; `false` si es el `SOURCE_COMMIT` que la plataforma puso en el entorno al arrancar, que puede estar desfasado |
| `mode` | `estándar`, `cloud` o `multiorg`, con acento |
| `llm` | `crm` si piensa por el CRM (`LLM_API_KEY` vacía, o multi-organización); `propia` si usa su `LLM_API_KEY`. Nunca la llave ni la URL |
| `webhookDeMeta.ultimoEn` | La última vez que Meta le mandó un webhook a Nea (ISO 8601, UTC), o `null`. Desde la 2.0 no debería pasar: si tiene fecha, el webhook todavía apunta a Nea y hay que devolvérselo al CRM (ver «Si vienes de Nea 1.x») |

Vocero raíz lee este `/health` para su tarjeta «Quién responde a tus clientes»
(pantalla Agente) si el CRM tiene `BRAIN_HEALTH_URL` con la dirección interna
de Nea, p. ej. `http://nea:8000/health`.

## Desarrollo local

Requisitos: Python 3.11+, un Postgres y un Vocero CRM con lo del paso 1.

```bash
git clone https://github.com/kevinrivm/nea-agent && cd nea-agent
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                            # llena los REEMPLAZA_...
uvicorn app.main:app --port 8000                # migraciones corren al arranque
```

Los mensajes entran por `POST /vocero/dispatch`. Un despacho a mano tiene que
llevar `x-vocero-signature: sha256=<HMAC-SHA256 del cuerpo crudo con
CRM_BOT_API_KEY>`; lo más simple es correr el CRM en local con
`BRAIN_DISPATCH_URL=http://127.0.0.1:8000/vocero/dispatch` y escribirle como
cliente (como hace `scripts/e2e_contra_raiz.py`).

### Probar en seco

- **Allowlist de pruebas**: con `ALLOWED_WA_IDS` poblada, Nea solo responde a
  esas identidades (lo demás queda en la bandeja del CRM sin respuesta). Vacíala
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
(Meta → CRM → despacho a Nea; Nea contesta por `/api/bot/*`) y hace de Meta y
de cliente: le manda al CRM webhooks con la forma real de la Cloud API,
firmados con `META_APP_SECRET`, y comprueba lo observable en la API del CRM,
su base y el outbox del wa-mock. Nada sale a Meta ni a WhatsApp: el CRM envía por su
wa-mock.

**Requisitos**: un checkout de [vocero-crm](https://github.com/kevinrivm/vocero-crm)
con `pnpm install` hecho (el guion lo arranca con `next dev` y le aplica las
migraciones), Node en el `PATH`, este repo con sus dependencias, y un Postgres
con una base (vacía o de una corrida anterior): el CRM en `public` y Nea en su
esquema `nea`, como se instala. El guion levanta y apaga el CRM y Nea; el
Postgres no.

**Variables** (del entorno o de uno o más `--env-file`; de esos archivos solo
se leen estas tres, así que puede ser el vault de operación):

| Variable | Qué es |
|---|---|
| `LLM_API_KEY` | Llave de OpenRouter. Nunca va por argv ni se imprime |
| `CRM_DATABASE_URL` | La base del par |
| `NEA_DATABASE_URL` | Opcional: otra base para Nea (también en el esquema `nea`) |

Lo demás (`BOT_API_KEY`, `META_APP_SECRET`, el token del webhook del CRM,
secretos de sesión y de cifrado) lo inventa cada corrida.

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
llega a la bandeja del CRM en segundos, el CRM se lo despacha a Nea y la
respuesta sale por el wa-mock sin Markdown; (2) `delivered` y `read` de Meta avanzan el estado del
mensaje en el CRM; (3) pedir horario trae huecos que el CRM registró como
ofrecidos, elegir uno crea la cita, el lead sube a la siguiente etapa abierta
y la confirmación dice «Enlace de la reunión» con la sala fija (no «Zoom»), y
Nea no afirma que un día «solo tiene mañana» por lo que no vio;
(4) Nea sabe a qué hora quedó la cita (bloque `booking` de `/api/bot/context`)
y no ofrece recordatorios, que el CRM raíz no manda;
(5) pedir una persona deja el handoff en la conversación y una despedida; (6)
tres rellenos seguidos cierran con una despedida y `cierre_sin_rumbo` en la
ficha, el relleno siguiente se calla y una pregunta con contenido reabre; (7)
con Nea apagada, el mensaje del cliente queda en la bandeja y, agotados los
reintentos del despacho, la conversación pasa a una persona con motivo
`error`; (8) un webhook de Meta que todavía llegue a Nea recibe 503 (para que
Meta reintente), queda en el log con qué hacer y en `/health`, y la
verificación de Meta ya no se contesta; (9) `/health` enseña versión, modo
`estándar` y ningún bloque `relay`.

En `--out` quedan una transcripción por cliente, `resultado.json` (evidencia
por escenario, latencia por turno con mediana y p95, gasto del modelo),
`medidor-llm.json` y los logs del CRM y de Nea. Sale con 0 si todo pasa, 1 si
algún escenario falla y 2 si no se pudo montar el par.

**Cuesta unos centavos de LLM** (~20 turnos contra `z-ai/glm-5.3-flash`) y
tarda unos 5 min; el escenario 7 es el más largo: apaga y vuelve a levantar
Nea y espera a que el CRM agote sus reintentos. Nea
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
pytest -q          # los de PgStore se saltan sin Postgres
```

Las de `tests/test_pg_store.py` corren `PgStore` contra un Postgres de verdad
(`MemoryStore` no tiene SQL ni filas que mapear, y ahí es donde se rompió el
relay de la 1.x), incluido el esquema propio (`DATABASE_SCHEMA`). Apúntalas
a un servidor desechable donde el usuario pueda crear bases: cada corrida crea
la suya, le aplica las migraciones y la borra al terminar.
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
| `AGENT_TIMEZONE` | `America/Mexico_City` | Zona horaria IANA de respaldo para las fechas del prompt; la de cada negocio la manda el CRM con sus citas |
| `BRIEF_PATH` | *(vacío)* | Markdown local con el brief del negocio (fallback) |
| `STALL_FILLER_STREAK` | `3` | Mensajes de relleno seguidos que cierran la conversación (0 = apagado) |
| `STALL_MAX_TURNS` | `14` | Mensajes del lead sin avanzar que la cierran (0 = apagado) |
| `STALL_COOLDOWN_HOURS` | `24` | Tras el cierre, cuánto se contesta el relleno con silencio |

## Licencia

[MIT](LICENSE) — igual que Vocero. Úsalo, véndelo instalado, modifícalo.

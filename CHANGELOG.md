# Cambios

Qué cambia en cada versión de Nea y qué hacer al actualizar. Cada tag
`vX.Y.Z` publica la imagen `ghcr.io/kevinrivm/nea-agent:X.Y.Z` (README,
«Instalar»).

## 2.0.2 — 2026-10-22

### Arreglado

- **Las notas de voz se transcriben sin configurar nada.** Pensando por el CRM
  (o con `LLM_BASE_URL`), el audio viaja dentro del chat y `whisper-1`, el
  default de `LLM_TRANSCRIBE_MODEL`, no existe ahí: toda nota de voz fallaba y
  Nea pedía que se la escribieran. Ahora, con `whisper-1` por esa vía, oye
  `google/gemini-3.5-flash-lite` (≈ USD 0.0001 por nota). Un modelo puesto en
  la variable se sigue respetando, y con OpenAI nada cambia.

### Seguridad

- **`pypdf` 6.19.0 → 6.21.2**: cierra ocho CVE publicadas en octubre de 2026
  (`pip-audit`). Es la librería que lee los PDF del conocimiento.

## 2.0.1 — 2026-10-21

### Arreglado

- **`ALLOWED_WA_IDS` y `TESTER_WA_IDS` aceptan el teléfono como se escriba**:
  con `+`, espacios, guiones o paréntesis. Antes, con el `+`, la lista no
  coincidía nunca con la identidad que manda el CRM (solo dígitos) y Nea no
  le contestaba ni al dueño en modo prueba. Hallado en el ensayo del starter
  3.0.0.

## 2.0.0 — 2026-10-20

Versión mayor: **hay algo que hacer a mano al actualizar** (abajo, «Actualizar
desde 1.x»). Spec 031 de vocero-crm (`specs/031-nea-por-despacho`).

### Cambió

- **Nea ya no recibe el webhook de Meta: el CRM se lo despacha.** Meta (y
  Zernio) le escriben al CRM; el CRM guarda el mensaje, junta la ráfaga y se la
  entrega a Nea firmada en `POST /vocero/dispatch`. Contra Vocero raíz la firma
  es `CRM_BOT_API_KEY`, la misma llave con la que Nea ya le contesta: no hay un
  secreto nuevo. Es la entrada que Vocero Cloud ya usaba.
- **Un CRM que no contesta al empezar el turno ya no se reintenta aquí.** Nea
  1.x repetía la ráfaga hasta diez minutos porque el relay podía no haberle
  entregado el mensaje al CRM todavía. Con el despacho, el CRM acaba de
  mandarlo: si no contesta, la cola del despacho le pasa la conversación a
  una persona (motivo `error`) en cuanto vuelva.

### Nuevo

- **Instagram, Messenger y TikTok.** Todo lo que el CRM despacha le llega a
  Nea, y el prompt nombra el canal (`conversation.channel`): un DM de
  Instagram no se contesta «por WhatsApp». En WhatsApp el chasis no cambia ni
  un byte.
- **Lo que el webhook daba, ahora llega del CRM**: el anuncio de origen
  (`adOrigen` del contexto), la ubicación, los nombres de las tarjetas de
  contacto y el nombre del documento (llaves nuevas del despacho, vocero-crm
  031). La nota de voz se reconoce por su tipo (`audio/ogg`).
- **`DATABASE_SCHEMA`**: con `nea`, Nea guarda sus tablas en ese esquema y
  puede usar la misma base del CRM. Vacía, `public`, como siempre.
- **Un mensaje repetido en dos despachos se lee una vez también contra la
  raíz** (antes, solo en cloud).
- `/health` gana `webhookDeMeta.ultimoEn`: cuándo le llegó a Nea un webhook de
  Meta, que desde la 2.0 ya no debería llegarle.
- **El Laboratorio del CRM prueba a Nea** (vocero-crm 033). Un despacho con
  `conversation.test: true` es una corrida del Laboratorio: pasa aunque la
  identidad (sintética) no esté en `ALLOWED_WA_IDS` y no programa seguimiento.
  La respuesta la guarda el CRM sin enviarla.
- **Roles como datos** (vocero-crm 034). El perfil del CRM trae `role` y
  `tools`: Nea le enseña al modelo solo las herramientas encendidas (ficha,
  agendar, reprogramar, salida; escalar va siempre). Sin «agendar», se porta
  como contra un CRM sin agenda. El cambio llega cuando Nea relee el perfil
  (cada 5 minutos), sin redesplegar. Sin `tools`, todas, como antes.
- **`LLM_API_KEY` es opcional junto a Vocero 2.0** (vocero-crm 036). Vacía,
  Nea piensa por el CRM: la llave vive en Ajustes → IA del CRM y no viaja
  hasta aquí; Nea llama a `{CRM_BASE_URL}/api/bot/llm` con `CRM_BOT_API_KEY`
  y usa el modelo que el perfil dice (`llm.model`), o `LLM_MODEL` si no trae.
  Cambiar el modelo en el CRM llega al releer el perfil (cada 5 minutos), sin
  redesplegar. Sin IA configurada en el CRM (402 `sin_credencial`), Nea calla
  y le pasa la conversación a una persona (motivo `error`). Con `LLM_API_KEY`
  puesta no cambia nada. Las notas de voz van por el mismo camino con
  `LLM_TRANSCRIBE_MODEL` (un modelo que oiga, no `whisper-1`). En modo cloud
  no cambia nada.
- `/health` gana `llm`: `"crm"` o `"propia"` (sin llaves ni URLs).
- **Nea mueve al lead de etapa** (vocero-crm 038): la herramienta
  `move_stage` llama a `POST /api/bot/stage`. Se le enseña al modelo solo con
  la clave `etapa` encendida en el perfil y si el contexto trae
  `lead.nextStages`, y solo hacia esas etapas. El prompt dice cuándo: en el
  mismo turno de la respuesta, en cuanto el lead dijo qué necesita o pidió
  avanzar (precio, contratar, empezar, agendar); nunca por un saludo o un
  «ok». Contra un CRM sin la ruta, la herramienta no existe.

### Se quitó

- `app/webhook.py`, `app/relay.py`, `app/coalesce.py`, el bloque `relay` de
  `/health` y las variables `META_APP_SECRET`, `VERIFY_TOKEN`,
  `CRM_WEBHOOK_URL`, `RELAY_BACKOFF_CAP_SECONDS`, `COALESCE_SECONDS`,
  `TYPING_DELAY_SECONDS` y `TURN_RETRY_DELAYS`. Si siguen en el entorno, Nea
  lo avisa al arrancar (solo los nombres, nunca los valores).
- La tabla `relay_queue` se queda, sin uso, por si guardaba algo sin entregar
  al actualizar. Se borra en una versión siguiente.
- `POST /webhook` responde **503** (no 404): si Meta todavía le escribe a Nea,
  reintenta en vez de dar el mensaje por entregado, y el log dice qué hacer.

### Actualizar desde 1.x

En este orden, para no perder mensajes:

1. **CRM** (Vocero 2.0): `BRAIN_DISPATCH_URL=https://<nea>/vocero/dispatch`
   (o `http://nea:8000/vocero/dispatch` en la misma red) y redeploy.
   `BOT_API_KEY` ya lo tiene. Después, **enciende el agente** en la pantalla
   Agente del CRM: desde la 2.0 ese interruptor decide si el CRM le despacha
   a Nea.
2. **Meta**: quita el override del número que apuntaba a Nea
   (`POST /{PHONE_NUMBER_ID}` con `"override_callback_uri": ""`). Los webhooks
   vuelven al override de la WABA, el del CRM. Compruébalo con
   `GET /{PHONE_NUMBER_ID}?fields=webhook_configuration`.
3. **Nea**: la imagen `2.0.0`. Puedes quitar las variables de «Se quitó» y,
   si quieres que piense con la llave de Ajustes → IA del CRM, también
   `LLM_API_KEY`.

Si haces el 3 antes que el 2, no se pierde nada: Nea le contesta 503 a Meta,
Meta reintenta, y `/health` enseña `webhookDeMeta.ultimoEn` hasta que muevas
el webhook.

Nea 1.x frente a un Vocero 2.0 sigue funcionando mientras no actualices:
`/api/bot/*` no cambió.

### Arreglado

- **La hora que ve el agente es la del negocio que atiende, no la de la
  instancia.** Una Nea en modo cloud atiende a negocios de zonas distintas,
  pero armaba el prompt con `AGENT_TIMEZONE` (la de México) para todos. Los
  horarios los calcula el CRM en la zona de cada negocio, así que la agenda
  estaba bien; lo que fallaba era lo que el agente decía de ella: a un lead de
  un negocio en Colombia le dijo «es de México el huso». Ahora la zona sale
  del bloque `booking` del contexto (la de Ajustes → Agenda) para el prompt,
  el seguimiento y las herramientas; «hoy», «mañana» y el calendario también
  son los del negocio. El prompt la nombra como lugar («hora de Colombia»),
  nunca con su nombre técnico, y el agente no le calcula al lead la
  diferencia con su hora. `AGENT_TIMEZONE` queda de respaldo para un CRM que
  no mande la zona. Sin migraciones ni variables nuevas.
- **Un mensaje del CRM se lee una sola vez, aunque venga en dos despachos.**
  En modo cloud, el CRM entrega la ráfaga completa cada vez que despacha: si
  el cliente escribe mientras un despacho va en vuelo o mientras Nea piensa,
  el siguiente despacho —con otro `dispatchId`— repite los mensajes
  anteriores. Nea deduplicaba solo por despacho y el repetido entraba otra vez
  a su historial («hola», y luego «hola» + la pregunta nueva). Ahora anota
  cada `messages[].id` cuando lo lee y descarta los que ya leyó; un despacho
  que no trae nada nuevo no gasta turno. Sin migraciones ni variables nuevas.

## 1.1.0 — 2026-10-08

Dos PR sobre 1.0.0: #36 y #38. Nada se rompe al actualizar.

### Nuevo

- **Nea olvida las conversaciones que el CRM elimina** (#36). En modo cloud,
  `POST /vocero/dispatch` acepta el evento `conversation.deleted` (misma
  firma que un despacho) y borra el historial, la fase, los horarios
  ofrecidos, los envíos pendientes y los despachos guardados de esa
  conversación, solo en la organización del aviso. El aviso pasa por la cola
  de despachos: un turno que esperaba termina antes, y un mensaje nuevo de la
  misma persona empieza después.
- **Una conversación nueva del CRM no hereda memoria** (#36), en los dos modos. Si
  la misma persona llega con un `conversation.id` distinto del que Nea
  recordaba, el turno empieza de cero. Es lo que cubre un aviso que no llegó.
- **Webhook con el secreto en la ruta: `/webhook/<VERIFY_TOKEN>`** (#38). Para
  instalar Nea en un servidor que no debe guardar el App Secret de la app de
  Meta (un Tech Provider que despliega para sus clientes: con ese secreto se
  administran los webhooks de TODA la app). Funciona como
  `/api/webhooks/wa/<token>` del CRM: ruta equivocada → 404, y la firma se
  verifica solo si `META_APP_SECRET` está puesta. Exige un `VERIFY_TOKEN` de
  32 caracteres o más; con uno más corto esa entrada no abre. `/webhook` no
  cambia: sigue exigiendo la firma.
- El `VERIFY_TOKEN` ya no queda en el log de accesos (ni en la ruta ni en el
  `hub.verify_token` del handshake) (#38).

### Actualizar

Basta con correr la versión nueva (`ghcr.io/kevinrivm/nea-agent:1.1.0`): no
hay migraciones ni variables nuevas, y `/webhook` se comporta igual que en
1.0.0.

- **Si tu Nea ya recibe por `/webhook` con `META_APP_SECRET`**, no tienes que
  cambiar nada.
- **Si quieres quitar el App Secret de ese servidor** (modo estándar): pon un
  `VERIFY_TOKEN` de 32 caracteres o más (`openssl rand -hex 32`), redespliega,
  cambia el override del número a `https://nea.tu-dominio.com/webhook/<VERIFY_TOKEN>`
  (README, «El webhook de Meta, a Nea») y, cuando un mensaje de prueba llegue,
  vacía `META_APP_SECRET` y redespliega otra vez. El proxy puede guardar la
  URL completa en sus propios logs, igual que con el webhook del CRM.
- **Comprueba**: `/health` con `"version": "1.1.0"`; un mensaje de prueba
  llega a la bandeja del CRM y Nea contesta.

## 1.0.0 — 2026-09-30

Primera versión con número y primera con imagen publicada. Hasta aquí Nea se
instalaba construyendo `main`. Lo de abajo cuenta desde el `main` del 18-sep
(`c90ec87`): los PR #27, #28, #29, #30 y #32. Si tu copia es más vieja, lee también
el final de «Actualizar».

> **¿Corres el modo estándar con un `main` de entre el 31-ago y el 21-sep?**
> Tu Nea no le está pasando los mensajes al CRM. Actualiza (ver «Actualizar»).

> **Modo estándar: `META_APP_SECRET` ahora es obligatoria.** Sin ella,
> `POST /webhook` responde 401 y Nea deja de recibir mensajes. Ponla antes de
> actualizar.

### Nuevo

- **Imagen en GHCR** (#29). Cada tag `vX.Y.Z` publica
  `ghcr.io/kevinrivm/nea-agent:X.Y.Z`, además de `X.Y` y `latest`, para
  `linux/amd64`. Coolify la descarga en vez de construirla en el VPS. El CI
  construye la imagen en cada PR y en cada push a `main`, sin publicarla.
- **`/health` dice qué Nea corre y cómo va el relay** (#28): `version`,
  `commit` y `commitVerified`, `mode` (`estándar`, `cloud` o `multiorg`) y, en
  modo estándar, `relay` con `pendientes`, `masViejoSegundos` y
  `ultimoErrorEn`. El código HTTP sigue dependiendo solo de la base.
- **El turno aguanta una caída del CRM** (#30). Si al empezar un turno el CRM
  no contesta (red, timeout, 5xx o 429), la misma ráfaga se reintenta tras
  `TURN_RETRY_DELAYS` (15 s, 45 s, 2 min y 5 min). Lo que el lead escriba
  mientras tanto sale en una sola respuesta. Si el CRM no vuelve a tiempo, la
  conversación pasa a un humano (handoff `error`) en cuanto conteste. Los
  reintentos viven en memoria: un reinicio de Nea los pierde; el mensaje no,
  porque el relay lo guarda en Postgres.
- **El cierre sin rumbo se ve en el CRM** (#28). Mientras dura, la ficha del
  contacto lleva `cierre_sin_rumbo` con la hora (el CRM raíz lo enseña como
  «Cierre sin rumbo»). Se borra al reabrir.
- **Prueba de punta a punta contra Vocero raíz** (#30).
  `scripts/e2e_contra_raiz.py` monta el par en modo estándar, hace de Meta y
  de cliente, y comprueba nueve historias en el CRM. Cuesta unos centavos de
  modelo.
- **Pruebas de `PgStore` contra un Postgres de verdad** (#27):
  `tests/test_pg_store.py`, con su propio job de CI (`postgres:16`).

### Cambió

- **`META_APP_SECRET` es obligatoria en modo estándar** (#32). Antes, vacía,
  `/webhook` aceptaba cualquier POST sin firma: quien conociera la URL podía
  hacerse pasar por un lead y hacer que Nea le contestara y gastara modelo.
  Ahora, sin secreto o con la firma ausente o inválida, responde 401. En modo
  cloud `/webhook` ya no se monta: la única entrada es el despacho firmado del
  CRM.
- **La imagen corre sin root** (#32): usuario `nea`, UID y GID 10001.
- **Límites de entrada** (#32). El cuerpo de `/webhook` y del despacho, hasta
  2 MiB (más, 413). La descarga de un adjunto, hasta 16 MiB y 30 s (antes,
  60 s). El texto de un PDF se extrae en un proceso aparte, con 6 s de tope y
  dos a la vez, para que un PDF pesado no frene los turnos de los demás.
- **El candado de cierre se reabre** (#28). Antes, tras la despedida había
  24 h de silencio pasara lo que pasara: un «¿cuánto cuesta?» se quedaba sin
  respuesta. Ahora el relleno («gracias», «ok 👍») se sigue contestando con
  silencio durante `STALL_COOLDOWN_HOURS`, pero un mensaje con contenido
  reabre en el acto y los contadores vuelven a cero. Los umbrales se ajustan
  con `STALL_MAX_TURNS`, `STALL_FILLER_STREAK` y `STALL_COOLDOWN_HOURS`, con
  los valores de siempre por defecto. Y «ok 👍» o «va, gracias 🙏» ahora
  cuentan como relleno.
- **Tope por intento al modelo** (#27): `LLM_TIMEOUT_SECONDS` (45 s), al
  conversar y al transcribir, y el SDK ya no reintenta por su cuenta. Antes un
  proveedor colgado podía tener al lead media hora en «escribiendo…» antes
  del handoff `error`.
- **La agenda se vuelve a preguntar** cada `AGENDA_PROBE_TTL_SECONDS` (60 s)
  (#27). Encender o apagar `AGENDA` en el CRM ya no exige reiniciar Nea.
- **El relay se pone al día en un minuto** (#27, #30). La espera entre
  intentos tiene tope de `RELAY_BACKOFF_CAP_SECONDS` (60 s; antes, 15 min), y
  un barrido vacía toda la cola vencida, no solo 50 filas.
- **Sin agenda v2, Nea no ofrece recordatorios** (#30). El CRM raíz no los
  manda: `book_session` ya no pide `recordatorios_aceptados` y el prompt dice
  que por aquí no hay recordatorios. Con la agenda v2 de Vocero Cloud todo
  sigue igual.
- **Un NUNCA más en `app/prompt.py`** (#30): no ofrecer ni prometer una acción
  que Nea no puede hacer con sus herramientas (llamar, mandar un correo,
  escribir más tarde, avisar, apartar un lugar). Ninguno de los NUNCA de antes
  se relajó.
- **La confirmación de la cita dice «Enlace de la reunión:»** (#27), no
  «Enlace de Zoom:»: el CRM no dice de qué proveedor es la sala.
- **Dependencias con versión exacta** en `requirements*.txt` (#27): lo que
  pasó las pruebas es lo que se despliega.

### Corregido

- **Modo estándar: el relay no le pasaba nada al CRM** (#27). Desde `95549c8`
  (31-ago, #12), `PgStore.due_relays` leía columnas que `relay_queue` no tiene
  y el worker se tragaba el error cada 5 s. Sin relay, ningún mensaje entrante
  llegaba a la bandeja, los estados de entrega no avanzaban, a un contacto
  nuevo Nea no le contestaba nunca (`/api/bot/context` daba 404) y a los
  conocidos solo mientras su último mensaje relevado tuviera menos de 24 h. El
  modo cloud no usa relay y no se vio afectado.
- **Markdown en WhatsApp** (#28). El lead veía `**$800**`, `## Precios`,
  tablas con barras y `[Agenda](https://…)`. Ahora `app/formato.py` lo
  convierte al formato de WhatsApp antes de enviar, en el turno y en el
  seguimiento, sin tocar URL ni correos. El historial guarda lo convertido.
- **Quien escribe sin compartir su número no recibía respuesta** (#28). Nea
  mandaba el BSUID pelón y el CRM lo guarda como `bsuid:<id>`: el contexto
  daba 404. `ALLOWED_WA_IDS` y `TESTER_WA_IDS` aceptan el BSUID con o sin el
  prefijo.
- **Un turno que revienta** por algo inesperado deja, además del log, un
  handoff `error` en la conversación (#27).
- **Un 404 del CRM ya no apaga la agenda de todo el proceso** (#27). Solo el
  404 vacío quiere decir «agenda apagada»; un 404 con el sobre de error del
  CRM es una conversación o una cita que no existe.
- **Contra un CRM que ignora la fecha pedida, Nea no niega la tarde** (#30).
  Dice qué horarios ve, avisa que es solo una parte y ofrece que el equipo
  confirme la hora o revisar otro día, en vez de «mañana solo tengo por la
  mañana».
- **El token del webhook del CRM ya no queda en el log de Nea** (#30). httpx
  escribía la URL de cada relay, con el token en la ruta.
- **Modo cloud: un despacho confirmado ya no se pierde si Nea se cae** (#32).
  Antes Nea respondía 200 al CRM con el turno solo en memoria. Ahora lo guarda
  en `dispatch_inbox` antes de confirmar y un worker lo retoma al arrancar. Un
  turno que se cortó a medias pasa a un humano en vez de repetirse, para no
  mandarle dos veces lo mismo al lead.

### Actualizar

Basta con correr la versión nueva: las migraciones se aplican solas y todas
las variables nuevas traen default. En modo cloud no hay relay: los puntos 2
y 6 no aplican.

**Antes, en modo estándar: comprueba que `META_APP_SECRET` tenga el App
Secret de tu app de Meta** (la misma del token). Si estaba vacía, desde esta
versión `POST /webhook` responde 401 y en el log de Nea aparece
`firma inválida o ausente en el webhook — 401`.

1. **¿Qué versión corres?** Si tu `/health` no trae `version`, tu Nea es de
   antes del 22-sep. Si dice `dev`, se construyó sin los build args.
2. **Modo estándar con un `main` de entre el 31-ago y el 21-sep** (de
   `95549c8` a antes de #27): el CRM no está recibiendo tus mensajes. En el
   log de Nea se ve como `relay: fallo procesando la cola` con
   `KeyError: 'organization_id'`, cada 5 s. Al actualizar, el primer barrido
   le entrega al CRM lo que tenga menos de 24 h y abandona lo más viejo (su
   cuerpo crudo se queda en `relay_queue`). A los leads que escribieron esos
   días y no recibieron respuesta nadie les contesta solo: revísalos en la
   bandeja.
3. **Pásate a la imagen** (recomendado). En Coolify, lo más limpio es una app
   nueva: + New → Docker Image, `ghcr.io/kevinrivm/nea-agent:1.0.0`, con las
   mismas variables, la misma base y el mismo dominio. Detén la vieja antes de
   arrancar la nueva: dos Nea contra la misma base reenvían dos veces lo
   pendiente. No pongas `NEA_VERSION` en las variables y deja `PORT` en 8000.
   La imagen corre como UID 10001: si montas el `BRIEF_PATH` desde un volumen,
   que ese usuario pueda leerlo.
   Si prefieres construir, hazlo desde el tag `v1.0.0` con los build args del
   README.
4. **Migraciones.** Se aplican solas al arrancar, antes de atender:
   - `006_stall_reapertura.sql`: `bot_conversation.stall_since_message_id`
     (`BIGINT NOT NULL DEFAULT 0`).
   - `007_relay_ultimo_error.sql`: `relay_queue.last_error_at` y su índice
     parcial.
   - `008_dispatch_inbox.sql`: la tabla `dispatch_inbox`. Solo la usa el modo
     cloud; en modo estándar se queda vacía.

   Son aditivas (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`): no
   borran ni reescriben datos.
5. **Variables nuevas.** Ninguna es obligatoria (la que cambió es
   `META_APP_SECRET`, arriba):

   | Variable | Default | Antes |
   |---|---|---|
   | `LLM_TIMEOUT_SECONDS` | `45` | Hasta 600 s por petición, y el SDK reintentaba dos veces más |
   | `AGENDA_PROBE_TTL_SECONDS` | `60` | Se preguntaba solo al arrancar |
   | `STALL_MAX_TURNS` | `14` | 14, fijo |
   | `STALL_FILLER_STREAK` | `3` | 3, fijo |
   | `STALL_COOLDOWN_HOURS` | `24` | 24, fijo |
   | `TURN_RETRY_DELAYS` | `15,45,120,300` | Sin reintentos: el turno se perdía |
   | `RELAY_BACKOFF_CAP_SECONDS` | `60` | 900 (15 min) |

6. **Si Meta le manda los webhooks a Nea por un override de la WABA, pásalo
   al del número** (README, «El webhook de Meta, a Nea»). Vocero raíz hasta
   1.3.0 lo borra cada vez que guardas la conexión de WhatsApp, y Nea deja de
   recibir sin aviso. Revisa a dónde llegan hoy con
   `GET /{PHONE_NUMBER_ID}?fields=webhook_configuration`.
7. **Si tu fork cambió `app/prompt.py`**, trae lo que agregó #30: el NUNCA de
   las acciones sin herramienta y el aviso de que no hay recordatorios. Es lo
   que evita que Nea prometa recordatorios que la raíz no manda.
8. **Comprueba**: `/health` con `"version": "1.0.0"`, `"mode": "estándar"` y
   `relay.pendientes` bajando a 0; un mensaje de prueba llega a la bandeja del
   CRM y Nea contesta.

**Si tu copia es anterior al 18-sep**, también te llegan:

- `LLM_REASONING_EFFORT` (`minimal`) y `LLM_PROVIDER_SORT` (`throughput`)
  (18-sep, #24). Van en cada llamada al modelo; OpenRouter los entiende, y si
  un proveedor los rechaza, Nea deja de mandarlos y reintenta. Vacíos, no se
  mandan.
- Las migraciones `004_multiorg.sql` y `005_pending_dispatch.sql` (31-ago), y
  la `003_stalled.sql` (19-ago). También se aplican solas.
- Los nombres `LLM_*` (31-ago, #11). Los `OPENAI_*` siguen funcionando.
- El modo cloud (31-ago, #9 y #12). Sin `VOCERO_MODE` no se activa nada.
- `TESTER_WA_IDS` (19-ago, #5). El comando `/reset` ya no cuelga de
  `ALLOWED_WA_IDS`: si lo usas, pon tu número también en `TESTER_WA_IDS`.

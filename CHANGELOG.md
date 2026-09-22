# Cambios

Qué cambia en cada versión de Nea y qué hacer al actualizar. Cada tag
`vX.Y.Z` publica la imagen `ghcr.io/kevinrivm/nea-agent:X.Y.Z` (README,
«Instalar»).

## 1.0.0 — 2026-09-XX

Primera versión con número y primera con imagen publicada. Hasta aquí Nea se
instalaba construyendo `main`. Lo de abajo cuenta desde el `main` del 18-sep
(`c90ec87`): los PR #27, #28, #29 y #30. Si tu copia es más vieja, lee también
el final de «Actualizar».

> **¿Corres el modo estándar con un `main` de entre el 31-ago y el 21-sep?**
> Tu Nea no le está pasando los mensajes al CRM. Actualiza (ver «Actualizar»).

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

### Actualizar

Basta con correr la versión nueva: las migraciones se aplican solas y todas
las variables nuevas traen default. En modo cloud no hay relay: los puntos 2
y 6 no aplican.

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
   Si prefieres construir, hazlo desde el tag `v1.0.0` con los build args del
   README.
4. **Migraciones.** Se aplican solas al arrancar, antes de atender:
   - `006_stall_reapertura.sql`: `bot_conversation.stall_since_message_id`
     (`BIGINT NOT NULL DEFAULT 0`).
   - `007_relay_ultimo_error.sql`: `relay_queue.last_error_at` y su índice
     parcial.

   Son aditivas (`ADD COLUMN IF NOT EXISTS`): no borran ni reescriben datos.
5. **Variables nuevas.** Ninguna es obligatoria:

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

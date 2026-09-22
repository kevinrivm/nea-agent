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

**El relay es lo que hace que el CRM vea el mensaje.** Cada POST de Meta se
encola en `relay_queue` (el Postgres de Nea) antes de parsear nada, y el
`RelayWorker` se lo reenvía crudo al webhook del CRM —firma intacta, backoff
hasta 24 h—. Si esa cola no sale, el CRM se queda sin los entrantes y sin los
estados de entrega, y a un contacto nuevo Nea no le contesta:
`/api/bot/context` responde 404 hasta que el relay aterriza. Como solo corre
contra Postgres, la cubren las pruebas de `tests/test_pg_store.py` (ver
Definición de Hecho).

Herramientas del LLM: `update_ficha` (calificación), `propose_slots` /
`book_session` (agenda), `route_out` (no califica; comparte los recursos
alternativos del perfil), `handoff` (pausa la IA en el CRM).

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

## Quickstart

Requisitos: Python 3.11+, Postgres propio (no el del CRM), una instancia de
Vocero CRM con el bot gateway habilitado (`BOT_API_KEY`), y una app de Meta
con WhatsApp Cloud API apuntando su webhook a este servicio.

**Si quieres que Nea agende, enciende el motor de agenda de Vocero**
(`AGENDA=on` en el CRM): viene apagado por defecto. Nea lo detecta al arrancar
y lo vuelve a preguntar cada minuto (`AGENDA_PROBE_TTL_SECONDS`), así que
encenderlo no exige reiniciarla — contra un CRM sin agenda no ofrece horarios
ni promete citas, califica y escala a un humano. La agenda la lleva el CRM:
Nea le pide los huecos, él registra lo ofrecido y solo acepta reservar uno de
esos.

```bash
git clone https://github.com/kevinrivm/nea-agent && cd nea-agent
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                            # llena los REEMPLAZA_...
uvicorn app.main:app --port 8000                # migraciones corren al arranque
```

Salud: `GET /health` responde 200 mientras la base conteste (503 si no), con
qué Nea es y cómo le va al relay:

```json
{"status": "ok", "db": "ok", "version": "1.4.0", "commit": "9f03997",
 "commitVerified": true, "mode": "estándar",
 "relay": {"pendientes": 0, "masViejoSegundos": null, "ultimoErrorEn": null}}
```

`mode` es `estándar`, `cloud` o `multiorg`, y `relay` solo aparece en el modo
de siempre (en cloud el CRM ya tiene el mensaje). `commit` va con
`commitVerified: true` solo si salió del build; el `SOURCE_COMMIT` que la
plataforma ponga en el entorno al arrancar viaja con `commitVerified: false`,
porque puede estar desfasado. Una cola atrasada no cambia el código HTTP: no
se arregla reiniciando el contenedor.

El webhook de Meta va a `GET|POST /webhook` con tu `VERIFY_TOKEN`.

### Docker / Coolify

El `Dockerfile` está listo para producción (healthcheck incluido). En Coolify:
app desde este repo + un Postgres, variables del `.env.example` en el runtime,
y el dominio del webhook hacia el puerto 8000. Para que `/health` diga qué
versión corre, pasa los build args `NEA_VERSION` y `SOURCE_COMMIT`
(`docker build --build-arg SOURCE_COMMIT=$(git rev-parse HEAD) …`).

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

## Definición de Hecho

Los tests unitarios (`pytest`, sin red ni Postgres) son el piso, no el techo.
"Hecho" = una conversación real multi-turno contra tu instancia, camino feliz
e infeliz (calificación, agenda, hostilidad, handoff), iterando hasta verde.
Los NUNCA del chasis en `app/prompt.py` no se relajan sin re-correr esa
verificación de comportamiento.

```bash
pytest -q          # 463 tests: 426 offline + 37 de PgStore, que se saltan sin Postgres
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

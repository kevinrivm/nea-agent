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

**Si quieres que Nea agende, enciende el motor de agenda de Vocero**
(`AGENDA=on` en el CRM): viene apagado por defecto. Nea lo detecta al arrancar
— contra un CRM sin agenda no ofrece horarios ni promete citas, califica y
escala a un humano. La agenda la lleva el CRM: Nea le pide los huecos, él
registra lo ofrecido y solo acepta reservar uno de esos.

```bash
git clone https://github.com/kevinrivm/nea-agent && cd nea-agent
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

### Probar en seco

- **Allowlist de pruebas**: con `ALLOWED_WA_IDS` poblada, Nea solo responde a
  esas identidades (todo lo demás se releva al CRM sin respuesta). Vacíala
  únicamente para salir a producción.
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
pytest -q          # 76 tests, todos offline
```

## Configuración

Todas las variables están documentadas en [`.env.example`](.env.example). Las
que definen la personalidad:

| Variable | Default | Qué hace |
|---|---|---|
| `AGENT_NAME` | `Nea` | Nombre del agente si el CRM no define uno |
| `AGENT_TIMEZONE` | `America/Mexico_City` | Zona horaria IANA para fechas del prompt |
| `BRIEF_PATH` | *(vacío)* | Markdown local con el brief del negocio (fallback) |

## Licencia

[MIT](LICENSE) — igual que Vocero. Úsalo, véndelo instalado, modifícalo.

# Hardening del lanzamiento — 2026-09-29

Cloud expone dispatch firmado y durable; no monta /webhook. Estándar requiere
META_APP_SECRET para validar Meta. No cambia la derivación HMAC del CRM.

La migración 008 persiste inbox por organización/dispatchId antes del ACK. Un
worker acotado toma un advisory lock por organización y registra el inicio antes
de efectos. Trabajo queued se recupera; started interrumpido se deriva a humano
sin repetir LLM/envío. Si handoff falla, la fila permanece recuperable. No se
promete exactly-once externo. El retry previo a leer contexto del webhook
estándar se conserva; cloud usa la política conservadora de handoff durable.

Cuerpo dispatch/webhook máximo 2 MiB; descarga 16 MiB/30 s; PDF en subproceso
sin credenciales, máximo 10 páginas/8000 caracteres, 6 s incluyendo espera y 2
concurrentes. Linux añade límites CPU/memoria. Docker corre UID/GID 10001.

Verificado: 503 pytest con PostgreSQL, compileall, Docker build/UID y prueba HTTP
multiturno con uvicorn+PostgreSQL, HMAC, aislamiento, dedup y recuperación después
de matar el proceso. Proveedores HTTP deterministas; no mensajes reales ni gasto.
CI reproduce esas comprobaciones y pip-audit. La resolución actual no muestra
avisos conocidos; sigue pendiente un lock de transitivas con hashes.

`python scripts/live-security-test.py` necesita TEST_DATABASE_URL local. Dentro
del contenedor dev se permite `--dev-runtime` solamente si CRM_BASE_URL apunta a
vocero-cloud-dev o dev.vocerocrm.com. Crea/elimina su propia base aleatoria y no
usa las conversaciones de la app. No imprime credenciales.

## Actualización 2.0 (spec 031 de vocero-crm, 2026-10-19)

El despacho firmado y durable es ahora la única entrada, también en estándar
(firma con `CRM_BOT_API_KEY`). `/webhook` ya no valida a Meta: responde 503 para
que Meta reintente mientras quien actualiza le devuelve el webhook al CRM, y
`META_APP_SECRET` dejó de leerse. El retry previo a leer el contexto se quitó:
estándar usa la misma política de handoff durable que cloud.

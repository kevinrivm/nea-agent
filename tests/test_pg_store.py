"""`PgStore` contra un Postgres de verdad.

## Por qué existe

Todo el resto de la suite corre con `MemoryStore`, que devuelve el objeto que
guardó: ahí no hay SQL que equivocar ni fila que mapear mal. Y justo ahí es
donde se rompió dos veces:

- `_pending_send_desde_fila` se quedó sin leer las columnas de organización de
  004, y la cola de «jamás se descarta» murió en silencio.
- Desde 95549c8, `due_relays` leía `organization_id`/`organization_slug`, que
  `relay_queue` NO tiene. `KeyError` en la primera fila pendiente, tragado cada
  5 s por `RelayWorker.run`: en el modo de siempre ningún entrante llegaba a la
  bandeja del CRM, los estados de entrega no se movían y, con un contacto
  nuevo, `/api/bot/context` daba 404 y Nea no contestaba nunca.

Las dos habrían salido aquí en el primer `SELECT`.

## Cómo se corre

    TEST_DATABASE_URL=postgresql://usuario:clave@host:5432/postgres pytest tests/test_pg_store.py

La URL es de un servidor DESECHABLE donde ese usuario pueda crear bases. Cada
corrida crea una base nueva (UTF8, desde `template0`), le aplica las
migraciones con `PgStore.migrate` —dos veces, porque el arranque las re-aplica
siempre— y la borra al terminar. Cada prueba empieza con las tablas vacías.

Sin la variable, el módulo se salta entero (la suite de siempre sigue sin red
ni Postgres). Con la variable puesta y el servidor caído, FALLA: un CI mal
configurado no puede pasar en verde sin haber probado nada.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest

from app.crm import CrmClient
from app.db import PgStore
from app.main import MIGRATIONS_DIR, create_app
from app.relay import RelayWorker
from app.sender import SenderWorker
from app.state import (
    AppContext,
    BotMessage,
    Conversation,
    OfferedSlot,
    PendingSend,
    RelayItem,
    RelayStats,
)
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    CRM_WEBHOOK_URL,
    IDENTITY,
    FakeLLM,
    crm_context,
    make_settings,
    mock_crm_basics,
    wa_body,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="sin TEST_DATABASE_URL: PgStore se prueba contra un Postgres de verdad",
)

# Todas las tablas de Nea. Si una migración añade otra, va aquí: si no, sus
# filas sobrevivirían de una prueba a la siguiente.
TABLAS = (
    "bot_message",
    "offered_slots",
    "pending_send",
    "bot_conversation",
    "processed_message",
    "relay_queue",
)


# ───────────────────────────────────────────────────── la base de prueba ───


def _en_su_propio_loop(corrutina: Coroutine[Any, Any, Any]) -> Any:
    """Corre `corrutina` en un loop nuevo, en otro hilo.

    La base vive lo que vive el módulo y pytest-asyncio le da a cada prueba su
    propio loop; una conexión de asyncpg no sobrevive al loop en que nació.
    Crearla y borrarla fuera de los loops de las pruebas evita que se pisen.
    """
    with ThreadPoolExecutor(max_workers=1) as hilo:
        return hilo.submit(asyncio.run, corrutina).result()


def _con_base(url: str, base: str) -> str:
    """La misma URL (usuario, host, parámetros), apuntando a otra base."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{base}"))


async def _crear_base(nombre: str) -> None:
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await admin.execute(
            f'CREATE DATABASE "{nombre}" TEMPLATE template0 '
            "ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
        )
    finally:
        await admin.close()
    # El camino de producción: el lifespan conecta y migra con estas mismas
    # dos llamadas. Dos veces, porque cada arranque las vuelve a aplicar.
    store = PgStore(_con_base(TEST_DATABASE_URL, nombre))
    await store.connect()
    try:
        await store.migrate(MIGRATIONS_DIR)
        await store.migrate(MIGRATIONS_DIR)
    finally:
        await store.aclose()


async def _borrar_base(nombre: str) -> None:
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture(scope="module")
def base_de_prueba():
    nombre = f"nea_test_{uuid.uuid4().hex[:12]}"
    _en_su_propio_loop(_crear_base(nombre))
    try:
        yield _con_base(TEST_DATABASE_URL, nombre)
    finally:
        _en_su_propio_loop(_borrar_base(nombre))


@pytest.fixture
async def store(base_de_prueba):
    s = PgStore(base_de_prueba)
    await s.connect()
    await s.pool.execute(f"TRUNCATE {', '.join(TABLAS)} RESTART IDENTITY CASCADE")
    try:
        yield s
    finally:
        await s.aclose()


async def _ahora(store: PgStore) -> datetime:
    """La hora del SERVIDOR: los DEFAULT now() salen de su reloj, no del nuestro."""
    return await store.pool.fetchval("SELECT clock_timestamp()")


def _ctx(store: PgStore, **ajustes: Any) -> AppContext:
    settings = make_settings(**ajustes)
    return AppContext(
        settings=settings,
        store=store,
        crm=CrmClient(settings.crm_base_url, settings.crm_bot_api_key),
        llm=FakeLLM(),
    )


# ──────────────────────────────────────────────────────────── migraciones ───


async def _columnas(store: PgStore, tabla: str) -> set[str]:
    filas = await store.pool.fetch(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = $1
        """,
        tabla,
    )
    return {f["column_name"] for f in filas}


@pytest.mark.parametrize(
    "tabla, modelo",
    [
        ("relay_queue", RelayItem),
        ("bot_conversation", Conversation),
        ("bot_message", BotMessage),
        ("offered_slots", OfferedSlot),
        ("pending_send", PendingSend),
    ],
)
async def test_cada_campo_del_modelo_tiene_su_columna(store, tabla, modelo):
    """El contrato entre `state.py` y `migrations/`, en las dos direcciones
    que importan: un campo nuevo sin migración se pone rojo aquí, y el mapeo
    que lee una columna que no existe se pone rojo en los viajes de abajo."""
    columnas = await _columnas(store, tabla)
    faltan = {c.name for c in fields(modelo)} - columnas
    assert not faltan, f"{tabla} no tiene columna para {sorted(faltan)}"


async def test_relay_queue_no_tiene_organizacion(store):
    """El relay es del modo de siempre, donde la organización es la única que
    hay. Leer estas columnas fue exactamente el fallo de 95549c8."""
    columnas = await _columnas(store, "relay_queue")
    assert "organization_id" not in columnas
    assert "organization_slug" not in columnas


async def test_re_migrar_con_datos_no_pierde_nada(store):
    """Cada arranque re-aplica TODAS las migraciones sobre una base con datos."""
    conv = await store.get_or_create_conversation(IDENTITY, "org_a", "a")
    await store.add_message(conv.id, "user", "hola")
    await store.enqueue_relay(b"{}", None)

    await store.migrate(MIGRATIONS_DIR)

    otra_vez = await store.get_or_create_conversation(IDENTITY, "org_a", "a")
    assert otra_vez.id == conv.id
    assert [m.content for m in await store.recent_messages(conv.id, 10)] == ["hola"]
    assert len(await store.due_relays(await _ahora(store))) == 1


# ─────────────────────────────────────────────────────────────── dedup ───


async def test_dedup_gana_solo_el_primero(store):
    assert await store.mark_processed("wamid.1") is True
    assert await store.mark_processed("wamid.1") is False
    assert await store.mark_processed("wamid.2") is True


# ─────────────────────────────────────────────────────────────── relay ───


async def test_encolar_y_leer_el_relay_devuelve_la_fila_entera(store):
    """EL fallo: esta lectura reventaba con KeyError desde 95549c8."""
    cuerpo = '{"entry":[{"texto":"ñandú 🧉"}]}'.encode()
    rid = await store.enqueue_relay(cuerpo, "sha256=abc")

    [item] = await store.due_relays(await _ahora(store))

    assert isinstance(item, RelayItem)
    assert item.id == rid
    assert item.body == cuerpo  # bytes EXACTOS: el CRM re-verifica la firma
    assert isinstance(item.body, bytes)
    assert item.signature == "sha256=abc"
    assert item.attempts == 0
    assert item.created_at.tzinfo is not None
    assert item.next_retry_at.tzinfo is not None
    assert item.delivered_at is None and item.abandoned_at is None


async def test_relay_sin_firma(store):
    await store.enqueue_relay(b"{}", None)
    [item] = await store.due_relays(await _ahora(store))
    assert item.signature is None


async def test_reprogramar_saca_el_relay_de_la_cola_hasta_su_hora(store):
    rid = await store.enqueue_relay(b"{}", None)
    ahora = await _ahora(store)
    await store.reschedule_relay(rid, 3, ahora + timedelta(minutes=5))

    assert await store.due_relays(ahora) == []
    [item] = await store.due_relays(ahora + timedelta(minutes=6))
    assert item.attempts == 3
    assert item.next_retry_at == ahora + timedelta(minutes=5)


async def test_el_estado_del_relay_para_health(store):
    """Lo que /health enseña: pendientes, edad del más viejo, último error."""
    assert await store.relay_stats() == RelayStats(0, None, None)
    fallido = await store.enqueue_relay(b"{}", None)
    await store.enqueue_relay(b"{}", None)
    await store.mark_relay_delivered(await store.enqueue_relay(b"{}", None))
    await store.mark_relay_abandoned(await store.enqueue_relay(b"{}", None))
    ahora = await _ahora(store)
    await store.reschedule_relay(fallido, 1, ahora + timedelta(seconds=30))
    await store.pool.execute(
        "UPDATE relay_queue SET created_at = now() - interval '5 minutes' WHERE id = $1",
        fallido,
    )

    stats = await store.relay_stats()

    assert stats.pendientes == 2  # ni el entregado ni el abandonado
    assert 300 <= stats.mas_viejo_segundos < 400
    assert stats.ultimo_error_en is not None
    assert stats.ultimo_error_en.tzinfo is not None
    [item] = [i for i in await store.due_relays(ahora + timedelta(minutes=1)) if i.id == fallido]
    assert item.last_error_at == stats.ultimo_error_en


async def test_entregado_o_abandonado_ya_no_se_reintenta(store):
    entregado = await store.enqueue_relay(b'{"a":1}', None)
    abandonado = await store.enqueue_relay(b'{"b":2}', None)
    vivo = await store.enqueue_relay(b'{"c":3}', None)

    await store.mark_relay_delivered(entregado)
    await store.mark_relay_abandoned(abandonado)

    assert [i.id for i in await store.due_relays(await _ahora(store))] == [vivo]
    marcas = await store.pool.fetch(
        "SELECT id, delivered_at, abandoned_at FROM relay_queue ORDER BY id"
    )
    assert marcas[0]["delivered_at"] is not None
    assert marcas[1]["abandoned_at"] is not None


async def test_el_relay_sabe_si_aun_guarda_el_payload_de_una_rafaga(store):
    """Lo que separa «el CRM no conoce a este lead» de «todavía no le llega su
    mensaje» cuando `/api/bot/context` da 404 (app/turn.py)."""
    pendiente = await store.enqueue_relay(wa_body(text="hola", wamid="wamid.pg.1"), None)
    entregado = await store.enqueue_relay(wa_body(text="hola", wamid="wamid.pg.2"), None)
    await store.mark_relay_delivered(entregado)

    assert await store.relay_pendiente_con(["wamid.pg.1"]) is True
    assert await store.relay_pendiente_con(["wamid.otro", "wamid.pg.1"]) is True
    assert await store.relay_pendiente_con(["wamid.pg.2"]) is False  # ya entregado
    assert await store.relay_pendiente_con([]) is False
    await store.mark_relay_abandoned(pendiente)
    assert await store.relay_pendiente_con(["wamid.pg.1"]) is False


async def test_adelantar_el_relay_lo_pone_a_tocar_ya(store):
    ahora = await _ahora(store)
    en_espera = await store.enqueue_relay(b"{}", None)
    await store.reschedule_relay(en_espera, 4, ahora + timedelta(minutes=10))
    await store.mark_relay_delivered(await store.enqueue_relay(b"{}", None))

    assert await store.due_relays(ahora) == []
    assert await store.adelantar_relays(ahora) == 1  # el entregado no cuenta
    [item] = await store.due_relays(ahora)
    assert item.id == en_espera and item.attempts == 4


async def test_la_cola_sale_en_orden_y_en_tandas_de_50(store):
    ids = [await store.enqueue_relay(f'{{"n":{n}}}'.encode(), None) for n in range(55)]
    tanda = await store.due_relays(await _ahora(store))
    assert [i.id for i in tanda] == ids[:50]


async def test_el_worker_entrega_al_crm_con_postgres(store, respx_mock):
    """`RelayWorker.process_due` de punta a punta contra la base real."""
    crm = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(200))
    cuerpo = b'{"object":"whatsapp_business_account","entry":[]}'
    rid = await store.enqueue_relay(cuerpo, "sha256=firma-de-meta")

    worker = RelayWorker(store, CRM_WEBHOOK_URL, asyncio.Event())
    try:
        await worker.process_due(now=await _ahora(store))
    finally:
        await worker.aclose()

    assert crm.call_count == 1
    assert crm.calls[0].request.content == cuerpo
    assert crm.calls[0].request.headers["x-hub-signature-256"] == "sha256=firma-de-meta"
    fila = await store.pool.fetchrow("SELECT * FROM relay_queue WHERE id = $1", rid)
    assert fila["delivered_at"] is not None
    assert await store.due_relays(await _ahora(store)) == []


async def test_el_worker_reprograma_con_backoff_si_el_crm_falla(store, respx_mock):
    respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(503))
    rid = await store.enqueue_relay(b"{}", None)

    worker = RelayWorker(store, CRM_WEBHOOK_URL, asyncio.Event())
    ahora = await _ahora(store)
    try:
        await worker.process_due(now=ahora)
    finally:
        await worker.aclose()

    fila = await store.pool.fetchrow("SELECT * FROM relay_queue WHERE id = $1", rid)
    assert fila["attempts"] == 1
    assert fila["next_retry_at"] > ahora
    assert fila["delivered_at"] is None


async def test_el_worker_abandona_lo_de_mas_de_24_h(store, respx_mock):
    crm = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(200))
    rid = await store.enqueue_relay(b"{}", None)
    await store.pool.execute(
        "UPDATE relay_queue SET created_at = now() - interval '25 hours' WHERE id = $1",
        rid,
    )

    worker = RelayWorker(store, CRM_WEBHOOK_URL, asyncio.Event())
    try:
        await worker.process_due(now=await _ahora(store))
    finally:
        await worker.aclose()

    assert crm.call_count == 0
    fila = await store.pool.fetchrow("SELECT * FROM relay_queue WHERE id = $1", rid)
    assert fila["abandoned_at"] is not None


async def test_un_barrido_vacia_la_cola_atrasada_entera(store, respx_mock):
    """La cola que dejó el relay roto: miles de webhooks viejos delante de los
    nuevos. De a 50 por barrido (cada 5 s), los mensajes nuevos esperaban
    detrás; un barrido tiene que abandonar los viejos y entregar los nuevos."""
    crm = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(200))
    viejos = [await store.enqueue_relay(b'{"viejo":1}', None) for _ in range(120)]
    nuevos = [await store.enqueue_relay(b'{"nuevo":1}', None) for _ in range(7)]
    await store.pool.execute(
        "UPDATE relay_queue SET created_at = now() - interval '3 days' WHERE id = ANY($1::bigint[])",
        viejos,
    )

    worker = RelayWorker(store, CRM_WEBHOOK_URL, asyncio.Event())
    try:
        await worker.process_due(now=await _ahora(store))
    finally:
        await worker.aclose()

    assert crm.call_count == len(nuevos)  # los viejos ni se intentan
    conteo = await store.pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE abandoned_at IS NOT NULL) AS abandonados,
               count(*) FILTER (WHERE delivered_at IS NOT NULL) AS entregados
        FROM relay_queue
        """
    )
    assert (conteo["abandonados"], conteo["entregados"]) == (120, 7)
    assert await store.due_relays(await _ahora(store)) == []


def _estados_de_meta() -> bytes:
    """Un POST de Meta que solo trae estados de entrega: relay y nada más."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "entry1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "123"},
                                "statuses": [
                                    {
                                        "id": "wamid.saliente",
                                        "status": "read",
                                        "timestamp": "1752700000",
                                        "recipient_id": IDENTITY,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


async def _hasta(condicion, segundos: float = 5.0) -> None:
    limite = asyncio.get_running_loop().time() + segundos
    while not condicion():
        if asyncio.get_running_loop().time() > limite:
            return
        await asyncio.sleep(0.05)


async def test_los_estados_de_entrega_llegan_al_crm(store, respx_mock):
    """Del POST de Meta a la bandeja del CRM, con la cola en Postgres."""
    crm = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(200))
    ctx = _ctx(store)
    worker = RelayWorker(store, CRM_WEBHOOK_URL, ctx.relay_wake)
    tarea = asyncio.create_task(worker.run())
    app = create_app(ctx=ctx)
    cuerpo = _estados_de_meta()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
        ) as cliente:
            resp = await cliente.post("/webhook", content=cuerpo)
            assert resp.status_code == 200
            await _hasta(lambda: crm.called)
    finally:
        tarea.cancel()
        await asyncio.gather(tarea, return_exceptions=True)
        await worker.aclose()
        await ctx.crm.aclose()

    assert crm.call_count == 1
    assert crm.calls[0].request.content == cuerpo


async def test_un_contacto_nuevo_recibe_respuesta_porque_el_relay_llega(
    store, respx_mock
):
    """El síntoma que vería un negocio: el primer mensaje de alguien nuevo.

    El CRM no conoce la identidad hasta que le llega el relay, y mientras
    tanto `/api/bot/context` responde 404. Con el relay roto ese 404 no
    cambiaba nunca y Nea se quedaba callada con cada contacto nuevo.
    """
    rutas = mock_crm_basics(respx_mock)
    rutas["context"].mock(
        side_effect=lambda _req: (
            httpx.Response(200, json=crm_context())
            if rutas["relay"].called
            else httpx.Response(404)
        )
    )
    ctx = _ctx(store)
    worker = RelayWorker(store, CRM_WEBHOOK_URL, ctx.relay_wake)
    tarea = asyncio.create_task(worker.run())
    app = create_app(ctx=ctx)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
        ) as cliente:
            await cliente.post("/webhook", content=wa_body(text="hola, quiero info"))
            await _hasta(lambda: rutas["messages"].called)
            await asyncio.sleep(0.2)  # que el turno termine de guardar
    finally:
        tarea.cancel()
        await asyncio.gather(tarea, return_exceptions=True)
        await worker.aclose()
        if ctx.coalescer is not None:
            await ctx.coalescer.aclose()
        await ctx.crm.aclose()

    assert rutas["relay"].call_count == 1
    assert rutas["messages"].call_count == 1
    enviado = json.loads(rutas["messages"].calls[0].request.content)
    assert enviado["conversationId"] == CRM_CONV_ID
    # Y el turno dejó su rastro en Postgres, no solo en el CRM.
    conv = await store.get_or_create_conversation(IDENTITY)
    assert conv.crm_conversation_id == CRM_CONV_ID
    assert conv.greeted is True
    assert [m.role for m in await store.recent_messages(conv.id, 10)] == [
        "user",
        "assistant",
    ]


# ─────────────────────────────────────────────────────── conversaciones ───


async def test_crear_conversacion_con_sus_defaults(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    assert conv == Conversation(id=conv.id, wa_identity=IDENTITY)


async def test_la_misma_identidad_es_la_misma_conversacion(store):
    una = await store.get_or_create_conversation(IDENTITY)
    otra = await store.get_or_create_conversation(IDENTITY)
    assert una.id == otra.id


async def test_la_conversacion_es_de_organizacion_e_identidad(store):
    """004: la misma persona escribiéndole a dos negocios no comparte historial."""
    de_a = await store.get_or_create_conversation(IDENTITY, "org_a", "a")
    de_b = await store.get_or_create_conversation(IDENTITY, "org_b", "b")
    sola = await store.get_or_create_conversation(IDENTITY)
    assert len({de_a.id, de_b.id, sola.id}) == 3
    assert (de_a.organization_id, de_a.organization_slug) == ("org_a", "a")
    await store.add_message(de_a.id, "user", "soy cliente de A")
    assert await store.recent_messages(de_b.id, 10) == []


async def test_el_slug_se_refresca_si_el_miembro_lo_renombra(store):
    antes = await store.get_or_create_conversation(IDENTITY, "org_a", "viejo")
    despues = await store.get_or_create_conversation(IDENTITY, "org_a", "nuevo")
    assert despues.id == antes.id
    assert despues.organization_slug == "nuevo"


async def test_update_conversation_guarda_cada_columna(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    t = datetime(2026, 9, 21, 15, 30, 12, 123456, tzinfo=timezone.utc)
    await store.update_conversation(
        conv.id,
        crm_conversation_id=CRM_CONV_ID,
        phase="agendando",
        greeted=True,
        media_notice_sent=True,
        followup_due_at=t,
        followup_sent=True,
        last_inbound_at=t - timedelta(hours=1),
        stalled_at=t + timedelta(minutes=1),
        stall_since_message_id=42,
    )
    leida = await store.get_or_create_conversation(IDENTITY)
    assert leida.crm_conversation_id == CRM_CONV_ID
    assert leida.phase == "agendando"
    assert leida.greeted is True and leida.media_notice_sent is True
    assert leida.followup_due_at == t
    assert leida.followup_sent is True
    assert leida.last_inbound_at == t - timedelta(hours=1)
    assert leida.stalled_at == t + timedelta(minutes=1)
    assert leida.stall_since_message_id == 42

    # Y se pueden volver a vaciar (así reabre el turno una conversación).
    await store.update_conversation(conv.id, stalled_at=None, followup_due_at=None)
    leida = await store.get_or_create_conversation(IDENTITY)
    assert leida.stalled_at is None and leida.followup_due_at is None


async def test_update_conversation_rechaza_columnas_que_no_existen(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    with pytest.raises(ValueError, match="columnas desconocidas"):
        await store.update_conversation(conv.id, organization_id="otra")
    await store.update_conversation(conv.id)  # nada que hacer, no truena


async def test_reset_deja_la_conversacion_como_nueva(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    ahora = await _ahora(store)
    await store.update_conversation(
        conv.id,
        crm_conversation_id=CRM_CONV_ID,
        phase="cerrada",
        greeted=True,
        media_notice_sent=True,
        followup_due_at=ahora,
        followup_sent=True,
        stalled_at=ahora,
    )
    await store.add_message(conv.id, "user", "hola")
    await store.replace_offered_slots(
        conv.id, [OfferedSlot(conv.id, ahora + timedelta(days=1), None, "mañana")]
    )
    otra = await store.get_or_create_conversation("5215500000000")
    await store.add_message(otra.id, "user", "yo no me borro")

    await store.reset_conversation(conv.id)

    leida = await store.get_or_create_conversation(IDENTITY)
    assert (leida.phase, leida.greeted, leida.media_notice_sent) == (
        "descubrimiento",
        False,
        False,
    )
    assert leida.followup_due_at is None and leida.followup_sent is False
    assert leida.stalled_at is None
    # Igual que MemoryStore: la conversación del CRM sigue siendo la misma.
    assert leida.crm_conversation_id == CRM_CONV_ID
    assert await store.recent_messages(conv.id, 10) == []
    assert await store.get_offered_slots(conv.id) == []
    assert [m.content for m in await store.recent_messages(otra.id, 10)] == [
        "yo no me borro"
    ]


# ─────────────────────────────────────────────────────────────── mensajes ───


async def test_el_historial_trae_los_ultimos_en_orden(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    await store.add_message(conv.id, "user", "uno", wa_message_id="wamid.1")
    await store.add_message(conv.id, "assistant", "dos 🙌")
    await store.add_message(conv.id, "user", "tres, ¿cuánto cuesta?")

    ultimos = await store.recent_messages(conv.id, 2)

    assert [(m.role, m.content) for m in ultimos] == [
        ("assistant", "dos 🙌"),
        ("user", "tres, ¿cuánto cuesta?"),
    ]
    todos = await store.recent_messages(conv.id, 10)
    assert todos[0].wa_message_id == "wamid.1"
    assert todos[1].wa_message_id is None
    assert all(m.conversation_id == conv.id for m in todos)
    assert all(m.created_at.tzinfo is not None for m in todos)


# ────────────────────────────────────────────────────────── slots ofrecidos ───


async def test_los_slots_ofrecidos_se_reemplazan_y_salen_en_orden(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    base = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
    await store.replace_offered_slots(
        conv.id,
        [
            OfferedSlot(conv.id, base + timedelta(days=1), None, "miércoles 23, 09:00"),
            OfferedSlot(
                conv.id, base, base + timedelta(minutes=30), "hoy martes 22, 09:00"
            ),
        ],
    )
    leidos = await store.get_offered_slots(conv.id)
    assert [s.label for s in leidos] == ["hoy martes 22, 09:00", "miércoles 23, 09:00"]
    assert leidos[0].start_utc == base
    assert leidos[0].end_utc == base + timedelta(minutes=30)
    assert leidos[1].end_utc is None
    assert all(s.offered_at.tzinfo is not None for s in leidos)

    # Reemplazo completo: la oferta vigente es siempre la última.
    await store.replace_offered_slots(
        conv.id, [OfferedSlot(conv.id, base + timedelta(days=2), None, "jueves 24")]
    )
    assert [s.label for s in await store.get_offered_slots(conv.id)] == ["jueves 24"]

    await store.clear_offered_slots(conv.id)
    assert await store.get_offered_slots(conv.id) == []


async def test_los_slots_no_se_cruzan_entre_conversaciones(store):
    una = await store.get_or_create_conversation(IDENTITY)
    otra = await store.get_or_create_conversation("5215500000000")
    t = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
    await store.replace_offered_slots(una.id, [OfferedSlot(una.id, t, None, "a")])
    await store.replace_offered_slots(otra.id, [OfferedSlot(otra.id, t, None, "b")])
    await store.clear_offered_slots(una.id)
    assert [s.label for s in await store.get_offered_slots(otra.id)] == ["b"]


# ──────────────────────────────────────────────────────── envíos pendientes ───


async def test_el_envio_pendiente_vuelve_con_su_organizacion_y_su_despacho(store):
    """La hermana del fallo del relay: este mapeo ya se había comido columnas."""
    conv = await store.get_or_create_conversation(IDENTITY, "org_a", "negocio-a")
    pid = await store.enqueue_pending_send(
        conv.id, CRM_CONV_ID, "tu cita quedó 🙌", "org_a", "negocio-a", "dsp_9"
    )

    [p] = await store.due_pending_sends(await _ahora(store))

    assert p == PendingSend(
        id=pid,
        conversation_id=conv.id,
        crm_conversation_id=CRM_CONV_ID,
        content="tu cita quedó 🙌",
        attempts=0,
        created_at=p.created_at,
        next_retry_at=p.next_retry_at,
        organization_id="org_a",
        organization_slug="negocio-a",
        dispatch_id="dsp_9",
    )


async def test_el_envio_pendiente_de_siempre_va_sin_organizacion(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    await store.enqueue_pending_send(conv.id, CRM_CONV_ID, "hola")
    [p] = await store.due_pending_sends(await _ahora(store))
    assert (p.organization_id, p.organization_slug, p.dispatch_id) == ("", "", "")


async def test_el_envio_pendiente_se_reprograma_entrega_o_abandona(store):
    conv = await store.get_or_create_conversation(IDENTITY)
    reprogramado = await store.enqueue_pending_send(conv.id, CRM_CONV_ID, "a")
    entregado = await store.enqueue_pending_send(conv.id, CRM_CONV_ID, "b")
    abandonado = await store.enqueue_pending_send(conv.id, CRM_CONV_ID, "c")
    ahora = await _ahora(store)

    await store.reschedule_pending_send(reprogramado, 2, ahora + timedelta(minutes=1))
    await store.mark_pending_send_delivered(entregado)
    await store.mark_pending_send_abandoned(abandonado)

    assert await store.due_pending_sends(ahora) == []
    [p] = await store.due_pending_sends(ahora + timedelta(minutes=2))
    assert (p.id, p.attempts) == (reprogramado, 2)


async def test_el_sender_entrega_lo_pendiente_con_postgres(store, respx_mock):
    """`SenderWorker.tick` de punta a punta: sale, se marca y se recuerda."""
    crm = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )
    ctx = _ctx(store)
    conv = await store.get_or_create_conversation(IDENTITY)
    pid = await store.enqueue_pending_send(conv.id, CRM_CONV_ID, "perdón la demora")

    try:
        await SenderWorker(ctx).tick(now=await _ahora(store))
    finally:
        await ctx.crm.aclose()

    assert json.loads(crm.calls[0].request.content) == {
        "conversationId": CRM_CONV_ID,
        "text": "perdón la demora",
    }
    fila = await store.pool.fetchrow("SELECT * FROM pending_send WHERE id = $1", pid)
    assert fila["delivered_at"] is not None
    assert [m.content for m in await store.recent_messages(conv.id, 10)] == [
        "perdón la demora"
    ]


# ─────────────────────────────────────────────────────────── seguimiento ───


async def test_solo_se_debe_el_seguimiento_que_toca(store):
    ahora = await _ahora(store)

    async def conversacion(identidad: str, **campos: Any) -> int:
        c = await store.get_or_create_conversation(identidad)
        await store.update_conversation(c.id, **campos)
        return c.id

    vencido = await conversacion("521550000001", followup_due_at=ahora - timedelta(hours=1))
    mas_vencido = await conversacion("521550000002", followup_due_at=ahora - timedelta(hours=2))
    await conversacion("521550000003", followup_due_at=ahora + timedelta(hours=1))
    await conversacion(
        "521550000004", followup_due_at=ahora - timedelta(hours=1), followup_sent=True
    )
    await conversacion(
        "521550000005", followup_due_at=ahora - timedelta(hours=1), phase="cerrada"
    )
    await conversacion("521550000006")  # sin seguimiento programado

    debidos = await store.due_followups(ahora)

    assert [c.id for c in debidos] == [mas_vencido, vencido]  # el más viejo primero
    assert all(isinstance(c, Conversation) for c in debidos)


async def test_el_seguimiento_se_reclama_una_sola_vez(store):
    ahora = await _ahora(store)
    conv = await store.get_or_create_conversation(IDENTITY, "org_a", "a")
    await store.update_conversation(conv.id, followup_due_at=ahora - timedelta(minutes=1))

    assert await store.claim_followup(conv.id) is True
    assert await store.claim_followup(conv.id) is False
    assert await store.due_followups(ahora) == []
    # El worker necesita saber de quién era: la fila vuelve con su organización.
    await store.update_conversation(conv.id, followup_sent=False)
    [debido] = await store.due_followups(ahora)
    assert (debido.organization_id, debido.organization_slug) == ("org_a", "a")


# ──────────────────────────────────────────────────────────────────── misc ───


async def test_ping_y_cierre(base_de_prueba):
    s = PgStore(base_de_prueba)
    with pytest.raises(RuntimeError, match="sin conectar"):
        _ = s.pool
    await s.connect()
    await s.ping()
    await s.aclose()
    await s.aclose()  # cerrar dos veces no truena

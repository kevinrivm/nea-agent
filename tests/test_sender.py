"""Envíos que agotan reintentos NO se descartan: se encolan y salen después.

Cubre el fix del incidente 2026-08-03 (respuesta generada perdida cuando Meta
hipa): el turno encola en pending_send y el SenderWorker entrega con backoff.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import timedelta

import httpx

from app import turn
from app.crm_brains import BrainsCrmClient
from app.db import _pending_send_desde_fila
from app.sender import SenderWorker
from app.state import AppContext, MemoryStore, PendingSend, utcnow
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    FakeLLM,
    make_ctx,
    make_settings,
)


def _ctx_cerebro() -> AppContext:
    """Una Nea contestando por `/api/brains`, con su organización puesta.

    El `make_ctx` de siempre arma un cliente de `/api/bot`, que no manda
    despacho ninguno: contra él estas pruebas pasarían sin probar nada.
    """
    return AppContext(
        settings=make_settings(),
        store=MemoryStore(),
        crm=BrainsCrmClient(CRM_URL, "secreto-del-despliegue", "negocio-a"),
        llm=FakeLLM(),
        organizacion=("org_a", "negocio-a"),
    )


async def _sin_esperas(monkeypatch):
    """Anula los sleeps del backoff del turno para no alentar la suite."""

    async def instantaneo(_seconds: float) -> None:
        return None

    monkeypatch.setattr(turn.asyncio, "sleep", instantaneo)


async def test_envio_agotado_se_encola_no_se_descarta(respx_mock, monkeypatch):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    route = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(502, json={"code": "meta_unavailable"})
    )
    await _sin_esperas(monkeypatch)

    sent = await turn._send(ctx, conv.id, CRM_CONV_ID, "respuesta importante")

    assert sent is False
    assert route.call_count == turn.SEND_ATTEMPTS  # paciencia: 4 intentos
    pendientes = await ctx.store.due_pending_sends(utcnow())
    assert len(pendientes) == 1
    assert pendientes[0].content == "respuesta importante"
    assert pendientes[0].crm_conversation_id == CRM_CONV_ID
    await ctx.crm.aclose()


async def test_la_cola_guarda_con_que_credencial_y_que_despacho(
    respx_mock, monkeypatch
):
    """Lo que el worker necesitará cuando el turno ya no exista.

    Despierta horas después, y para entonces no hay despacho del que sacar
    nada: las dos cosas tienen que estar en la fila.

    - **La organización**, para hablarle al CRM con la credencial de ESE
      negocio y no con la de otro.
    - **El despacho**, porque `/api/brains/messages` lo exige. Sin él, cada
      reintento diferido se rechaza con 422 hasta agotar las 24 h: la cola
      entera —la del incidente 2026-08-03— existiría para nada.
    """
    ctx = _ctx_cerebro()
    ctx.crm.registrar_despacho(CRM_CONV_ID, "dsp_abc")
    conv = await ctx.store.get_or_create_conversation(IDENTITY, "org_a", "negocio-a")
    respx_mock.post(f"{CRM_URL}/api/brains/messages").mock(
        return_value=httpx.Response(502, json={"code": "meta_unavailable"})
    )
    await _sin_esperas(monkeypatch)

    await turn._send(ctx, conv.id, CRM_CONV_ID, "respuesta importante")

    pendiente = (await ctx.store.due_pending_sends(utcnow()))[0]
    assert pendiente.organization_id == "org_a"
    assert pendiente.organization_slug == "negocio-a"
    assert pendiente.dispatch_id == "dsp_abc"
    await ctx.crm.aclose()


async def test_el_reintento_diferido_manda_el_despacho_de_su_fila(respx_mock):
    """Y el worker lo usa. De punta a punta, que es donde se rompió."""
    ctx = _ctx_cerebro()
    conv = await ctx.store.get_or_create_conversation(IDENTITY, "org_a", "negocio-a")
    await ctx.store.enqueue_pending_send(
        conv.id, CRM_CONV_ID, "respuesta importante", "org_a", "negocio-a", "dsp_abc"
    )
    ruta = respx_mock.post(f"{CRM_URL}/api/brains/messages").mock(
        return_value=httpx.Response(201, json={"message": {"id": "msg_1"}})
    )

    await SenderWorker(ctx).tick()

    import json as _json

    assert _json.loads(ruta.calls.last.request.content)["dispatchId"] == "dsp_abc"
    assert await ctx.store.due_pending_sends(utcnow()) == []  # entregado
    await ctx.crm.aclose()


async def test_409_no_se_encola(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(409, json={"error": {"code": "ai_paused"}})
    )

    sent = await turn._send(ctx, conv.id, CRM_CONV_ID, "hola")

    assert sent is False
    assert await ctx.store.due_pending_sends(utcnow()) == []  # silencio, sin cola
    await ctx.crm.aclose()


async def test_sender_entrega_pendiente_y_lo_recuerda(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "hola tarde")
    route = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    worker = SenderWorker(ctx)

    await worker.tick()

    assert route.call_count == 1
    assert ctx.store.pending_sends[pid].delivered_at is not None
    history = await ctx.store.recent_messages(conv.id, 10)
    assert [(m.role, m.content) for m in history] == [("assistant", "hola tarde")]
    # segundo barrido: ya no hay nada que enviar
    await worker.tick()
    assert route.call_count == 1
    await ctx.crm.aclose()


async def test_sender_reintenta_con_backoff(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "texto")
    route = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(502, json={"code": "meta_unavailable"})
    )
    worker = SenderWorker(ctx)

    await worker.tick()

    item = ctx.store.pending_sends[pid]
    assert route.call_count == 1
    assert item.attempts == 1
    assert item.next_retry_at > utcnow()  # reprogramado, no martillado
    # aún no toca reintentar: el barrido siguiente no lo levanta
    await worker.tick()
    assert route.call_count == 1
    await ctx.crm.aclose()


async def test_sender_abandona_por_409_sin_handoff(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "texto")
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(409, json={"error": {"code": "window_closed"}})
    )
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )
    worker = SenderWorker(ctx)

    await worker.tick()

    assert ctx.store.pending_sends[pid].abandoned_at is not None
    assert handoff.call_count == 0  # rechazo legítimo: sin alerta
    await ctx.crm.aclose()


async def test_sender_agota_24h_abandona_y_alerta(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "texto")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(hours=25)
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )
    worker = SenderWorker(ctx)

    await worker.tick()

    assert messages.call_count == 0  # vencido: ni se intenta
    assert ctx.store.pending_sends[pid].abandoned_at is not None
    assert handoff.call_count == 1  # humano alertado vía handoff error
    await ctx.crm.aclose()


# ── El mapeo de la fila ───────────────────────────────────────────────────


def test_el_mapeo_de_pending_send_no_se_deja_ninguna_columna():
    """Toda columna que se guarda se vuelve a leer.

    El fallo que esto cierra: el mapeo iba campo a campo y se quedó sin leer
    `organization_id`/`organization_slug`, que 004 había añadido. La fila las
    guardaba bien y al releerla volvían vacías, así que el SenderWorker no
    sabía de quién era el envío y lo abandonaba. La cola de «jamás se
    descarta» estaba muerta y nadie se enteró.

    Se comprueba contra `fields()` y no contra una lista escrita a mano para
    que el próximo campo que alguien añada no pueda olvidarse: si no está en
    el mapeo, esta prueba se pone roja sola.

    `MemoryStore` no puede cubrir esto — devuelve el objeto que guardó, así que
    no hay mapeo que equivocar. Este error solo existe contra Postgres.
    """
    ahora = utcnow()
    fila = {
        "id": 7,
        "conversation_id": 3,
        "crm_conversation_id": "cv_abc",
        "content": "hola",
        "attempts": 2,
        "created_at": ahora,
        "next_retry_at": ahora,
        "delivered_at": None,
        "abandoned_at": None,
        "organization_id": "org_a",
        "organization_slug": "negocio-a",
        "dispatch_id": "dsp_1",
    }
    pendiente = _pending_send_desde_fila(fila)

    for campo in fields(PendingSend):
        assert getattr(pendiente, campo.name) == fila[campo.name], (
            f"el mapeo no lee {campo.name}"
        )

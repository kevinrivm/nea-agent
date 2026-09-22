"""Relay: body crudo + firma intactos, reintento con backoff, abandono a las 24 h."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx

from app.relay import RelayWorker
from app.state import utcnow
from tests.conftest import CRM_WEBHOOK_URL


async def test_relay_reintenta_ante_500_y_entrega(ctx, respx_mock):
    route = respx_mock.post(CRM_WEBHOOK_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200)]
    )
    body = b'{"payload": "crudo con bytes exactos"}'
    firma = "sha256=abc123"
    rid = await ctx.store.enqueue_relay(body, firma)

    worker = RelayWorker(ctx.store, CRM_WEBHOOK_URL, asyncio.Event())
    t0 = utcnow()
    await worker.process_due(now=t0)

    item = ctx.store.relays[rid]
    assert item.delivered_at is None
    assert item.attempts == 1
    assert item.next_retry_at > t0  # backoff programado

    # aún no toca reintentar
    await worker.process_due(now=t0 + timedelta(seconds=1))
    assert route.call_count == 1

    # pasado el backoff → entrega
    await worker.process_due(now=t0 + timedelta(seconds=10))
    assert route.call_count == 2
    assert ctx.store.relays[rid].delivered_at is not None

    # el CRM recibió los bytes EXACTOS y la firma original
    for call in route.calls:
        assert call.request.content == body
        assert call.request.headers["x-hub-signature-256"] == firma
    await worker.aclose()


async def test_relay_backoff_crece_exponencial(ctx, respx_mock):
    respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(503))
    rid = await ctx.store.enqueue_relay(b"{}", None)
    worker = RelayWorker(ctx.store, CRM_WEBHOOK_URL, asyncio.Event())

    t0 = utcnow()
    await worker.process_due(now=t0)
    delay1 = (ctx.store.relays[rid].next_retry_at - t0).total_seconds()
    t1 = t0 + timedelta(seconds=delay1 + 0.1)
    await worker.process_due(now=t1)
    delay2 = (ctx.store.relays[rid].next_retry_at - t1).total_seconds()
    assert delay2 > delay1  # exponencial
    await worker.aclose()


async def test_relay_abandona_tras_24h(ctx, respx_mock):
    route = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(200))
    rid = await ctx.store.enqueue_relay(b"{}", None)
    ctx.store.relays[rid].created_at = utcnow() - timedelta(hours=25)

    worker = RelayWorker(ctx.store, CRM_WEBHOOK_URL, asyncio.Event())
    await worker.process_due()
    assert route.call_count == 0  # ya ni lo intenta
    assert ctx.store.relays[rid].abandoned_at is not None
    await worker.aclose()


class _StoreEnLotes:
    """Devuelve la cola de a 3, como PgStore la devuelve de a 50."""

    def __init__(self, store) -> None:
        self._store = store

    def __getattr__(self, nombre):
        return getattr(self._store, nombre)

    async def due_relays(self, now):
        return (await self._store.due_relays(now))[:3]


async def test_un_barrido_no_se_queda_en_el_primer_lote(ctx, respx_mock):
    """Con una cola atrasada, los mensajes nuevos no esperan detrás de los viejos."""
    route = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(200))
    viejos = [await ctx.store.enqueue_relay(b"{}", None) for _ in range(7)]
    for rid in viejos:
        ctx.store.relays[rid].created_at = utcnow() - timedelta(days=3)
    nuevo = await ctx.store.enqueue_relay(b'{"nuevo":1}', None)

    worker = RelayWorker(_StoreEnLotes(ctx.store), CRM_WEBHOOK_URL, asyncio.Event())
    await worker.process_due()

    assert route.call_count == 1
    assert ctx.store.relays[nuevo].delivered_at is not None
    assert all(ctx.store.relays[r].abandoned_at is not None for r in viejos)
    await worker.aclose()


async def test_un_barrido_con_el_crm_caido_intenta_cada_uno_una_vez(ctx, respx_mock):
    """Lo que falla se reprograma al futuro: el barrido termina, no da vueltas."""
    route = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(503))
    ids = [await ctx.store.enqueue_relay(b"{}", None) for _ in range(8)]

    worker = RelayWorker(_StoreEnLotes(ctx.store), CRM_WEBHOOK_URL, asyncio.Event())
    await worker.process_due()

    assert route.call_count == 8
    assert all(ctx.store.relays[r].attempts == 1 for r in ids)
    await worker.aclose()


async def test_relay_sin_firma_no_manda_header(ctx, respx_mock):
    route = respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(200))
    await ctx.store.enqueue_relay(b'{"x":1}', None)
    worker = RelayWorker(ctx.store, CRM_WEBHOOK_URL, asyncio.Event())
    await worker.process_due()
    assert "x-hub-signature-256" not in route.calls[0].request.headers
    await worker.aclose()


async def _espera_tras_fallar(ctx, worker, intentos_previos: int) -> float:
    rid = await ctx.store.enqueue_relay(b"{}", None)
    ctx.store.relays[rid].attempts = intentos_previos
    t0 = utcnow()
    await worker.process_due(now=t0)
    return (ctx.store.relays[rid].next_retry_at - t0).total_seconds()


async def test_tras_una_caida_larga_el_relay_reintenta_cada_minuto(ctx, respx_mock):
    """El tope era de 15 min: con el CRM de vuelta, el mensaje tardaba hasta
    15 min más en llegar a la bandeja. Ahora, como mucho, un minuto."""
    respx_mock.post(CRM_WEBHOOK_URL).mock(side_effect=httpx.ConnectError("caído"))
    worker = RelayWorker(ctx.store, CRM_WEBHOOK_URL, asyncio.Event())
    assert await _espera_tras_fallar(ctx, worker, 2) == 8  # sigue creciendo al doble
    assert await _espera_tras_fallar(ctx, worker, 12) == 60
    await worker.aclose()


async def test_el_tope_del_relay_se_configura(ctx, respx_mock):
    respx_mock.post(CRM_WEBHOOK_URL).mock(return_value=httpx.Response(503))
    worker = RelayWorker(ctx.store, CRM_WEBHOOK_URL, asyncio.Event(), backoff_cap=20)
    assert await _espera_tras_fallar(ctx, worker, 12) == 20
    await worker.aclose()


def test_relay_backoff_cap_seconds_sale_del_entorno(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("RELAY_BACKOFF_CAP_SECONDS", raising=False)
    assert Settings(_env_file=None).relay_backoff_cap_seconds == 60
    monkeypatch.setenv("RELAY_BACKOFF_CAP_SECONDS", "30")
    assert Settings(_env_file=None).relay_backoff_cap_seconds == 30


async def test_al_entregar_lo_que_antes_fallo_avisa_que_el_crm_volvio(ctx, respx_mock):
    """Es lo que despierta a los turnos que esperaban al CRM (app/turn.py)."""
    respx_mock.post(CRM_WEBHOOK_URL).mock(
        side_effect=[httpx.Response(200), httpx.Response(503), httpx.Response(200)]
    )
    avisos: list[int] = []
    worker = RelayWorker(
        ctx.store, CRM_WEBHOOK_URL, asyncio.Event(), al_volver=lambda: avisos.append(1)
    )
    await ctx.store.enqueue_relay(b"{}", None)
    await worker.process_due(now=utcnow())
    assert avisos == []  # a la primera: el CRM nunca se fue
    await ctx.store.enqueue_relay(b"{}", None)
    t1 = utcnow()
    await worker.process_due(now=t1)  # 503
    assert avisos == []
    await worker.process_due(now=t1 + timedelta(seconds=10))  # entrega
    assert avisos == [1]
    await worker.aclose()

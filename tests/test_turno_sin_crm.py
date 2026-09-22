"""Un turno que no alcanza al CRM ya no se pierde.

En el e2e contra Vocero raíz (escenario 7) el CRM se apagó ~20 s: el turno
probó el contexto tres veces en dos segundos, calló sin handoff, y al volver
el CRM el mensaje apareció en la bandeja (lo entregó el relay) con la IA
encendida y nadie que le contestara.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, Callable

import httpx
import pytest

from app import turn
from app.state import InboundMessage
from tests.conftest import (
    CRM_URL,
    IDENTITY,
    crm_context,
    make_ctx,
    make_settings,
    mock_crm_basics,
    wa_body,
)

CONTEXT = f"{CRM_URL}/api/bot/context"


@pytest.fixture(autouse=True)
def _rapido(monkeypatch):
    # Los tiempos reales son de segundos y minutos; aquí, de milésimas.
    monkeypatch.setattr(turn, "CONTEXT_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(turn, "VIGILANCIA_SEGUNDOS", 0.05)
    monkeypatch.setattr(turn, "RELAY_ESPERA_SECONDS", 0.2)


def _ctx(delays: str = "0.05,0.1,0.15"):
    return make_ctx(make_settings(turn_retry_delays=delays))


def _msg(wamid: str, texto: str) -> InboundMessage:
    return InboundMessage(wa_message_id=wamid, identity=IDENTITY, type="text", text=texto)


async def _hasta(cond: Callable[[], Any], timeout: float = 3.0) -> Any:
    fin = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < fin:
        valor = cond()
        if valor:
            return valor
        await asyncio.sleep(0.02)
    return cond()


class CrmIntermitente:
    """`/api/bot/context` que falla (red o 5xx) mientras `caido` sea cierto."""

    def __init__(self, fallo: Callable[[], httpx.Response] | None = None) -> None:
        self.caido = True
        self.pedidos = 0
        self._fallo = fallo

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.pedidos += 1
        if self.caido:
            if self._fallo is None:
                raise httpx.ConnectError("el CRM está apagado", request=request)
            return self._fallo()
        return httpx.Response(200, json=crm_context())


def _usuario(ctx) -> str:
    """Lo que el modelo recibió como mensaje del lead en su primera llamada."""
    return [m for m in ctx.llm.calls[0]["messages"] if m["role"] == "user"][-1]["content"]


async def test_crm_caido_y_de_vuelta_el_lead_recibe_respuesta(respx_mock):
    """Por el webhook, con el dedup de por medio: el reintento no vuelve a
    pasar por él (el wamid ya está marcado y aun así se contesta)."""
    from app.main import create_app

    ctx = _ctx()
    routes = mock_crm_basics(respx_mock)
    crm = CrmIntermitente()
    routes["context"].mock(side_effect=crm)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test") as cliente:
        assert (await cliente.post("/webhook", content=wa_body(text="hola", wamid="wamid.caida"))).status_code == 200
        assert await _hasta(lambda: ctx.turnos_pendientes)  # esperando al CRM, no en silencio
        crm.caido = False
        assert await _hasta(lambda: routes["messages"].called)
        await asyncio.sleep(0.1)
    assert routes["messages"].call_count == 1
    assert routes["handoff"].call_count == 0
    assert "wamid.caida" in ctx.store.processed
    assert ctx.turnos_pendientes == {}
    assert len(ctx.llm.calls) == 1
    await ctx.crm.aclose()


async def test_un_5xx_tambien_es_pasajero(respx_mock):
    ctx = _ctx()
    routes = mock_crm_basics(respx_mock)
    crm = CrmIntermitente(lambda: httpx.Response(503))
    routes["context"].mock(side_effect=crm)
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.5xx", "hola")])
    assert IDENTITY in {k[1] for k in ctx.turnos_pendientes}
    crm.caido = False
    assert await _hasta(lambda: routes["messages"].called)
    assert routes["messages"].call_count == 1
    await ctx.crm.aclose()


async def test_un_404_de_verdad_sigue_siendo_silencio(respx_mock):
    """El CRM contestó que no conoce a este lead y el relay ya no tiene nada
    que entregarle: no hay a quién contestarle, y no se insiste."""
    ctx = _ctx()
    routes = mock_crm_basics(respx_mock)
    routes["context"].mock(return_value=httpx.Response(404, json={"error": {"code": "not_found"}}))
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.404", "hola")])
    await asyncio.sleep(0.3)
    assert routes["context"].call_count == turn.CONTEXT_ATTEMPTS
    assert routes["messages"].call_count == 0
    assert ctx.turnos_pendientes == {}
    await ctx.crm.aclose()


async def test_un_404_con_el_mensaje_aun_en_el_relay_empuja_al_relay_y_reintenta(respx_mock):
    """El CRM volvió pero el relay (en backoff) todavía no le entrega el
    mensaje: ese 404 dice «todavía no me llega», no «no existe»."""
    from datetime import timedelta

    from app.state import utcnow

    ctx = _ctx()
    routes = mock_crm_basics(respx_mock)
    rid = await ctx.store.enqueue_relay(wa_body(text="hola", wamid="wamid.relay"), None)
    ctx.store.relays[rid].next_retry_at = utcnow() + timedelta(seconds=60)
    routes["context"].mock(
        side_effect=lambda _r: (
            httpx.Response(200, json=crm_context())
            if ctx.store.relays[rid].delivered_at
            else httpx.Response(404, json={"error": {"code": "not_found"}})
        )
    )
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.relay", "hola")])
    # El relay ya no espera su backoff: le toca ahora y se le despertó.
    assert ctx.store.relays[rid].next_retry_at <= utcnow()
    assert ctx.relay_wake.is_set()
    assert ctx.turnos_pendientes
    ctx.store.relays[rid].delivered_at = utcnow()  # el relay entregó
    assert await _hasta(lambda: routes["messages"].called)
    assert routes["messages"].call_count == 1
    await ctx.crm.aclose()


async def test_si_el_relay_entrega_mientras_se_le_espera_se_contesta_en_ese_intento(respx_mock):
    """El e2e: el CRM volvió justo a media ráfaga, dio 404 y el relay
    entregó medio segundo después. Esperar al relay ahorra la vuelta entera
    de reintentos (45 s en esa corrida)."""
    from app.state import utcnow

    ctx = _ctx()
    routes = mock_crm_basics(respx_mock)
    rid = await ctx.store.enqueue_relay(wa_body(text="hola", wamid="wamid.justo"), None)
    routes["context"].mock(
        side_effect=lambda _r: (
            httpx.Response(200, json=crm_context())
            if ctx.store.relays[rid].delivered_at
            else httpx.Response(404, json={"error": {"code": "not_found"}})
        )
    )

    async def entregar() -> None:
        await asyncio.sleep(0.1)
        ctx.store.relays[rid].delivered_at = utcnow()

    entrega = asyncio.create_task(entregar())
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.justo", "hola")])
    await entrega
    assert routes["messages"].call_count == 1
    assert ctx.turnos_pendientes == {}
    await ctx.crm.aclose()


async def test_cuando_el_crm_vuelve_no_se_espera_el_resto_de_la_espera(respx_mock):
    """El relay avisa al entregar lo que antes no pudo (`reanudar_pendientes`):
    con 5 min de espera por delante, la respuesta sale ya."""
    ctx = _ctx("300")
    routes = mock_crm_basics(respx_mock)
    crm = CrmIntermitente()
    routes["context"].mock(side_effect=crm)
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.vuelve", "hola")])
    assert ctx.turnos_pendientes
    crm.caido = False
    assert turn.reanudar_pendientes(ctx) == 1
    assert await _hasta(lambda: routes["messages"].called, timeout=2.0)
    assert ctx.turnos_pendientes == {}
    await ctx.crm.aclose()


async def test_lo_que_escribe_durante_la_caida_se_contesta_una_sola_vez(respx_mock):
    """Dos ráfagas con el CRM caído: al volver, UN turno las contesta juntas
    (antes, cada una habría abierto su propia respuesta)."""
    ctx = _ctx("0.3,0.3,0.3")
    routes = mock_crm_basics(respx_mock)
    crm = CrmIntermitente()
    routes["context"].mock(side_effect=crm)
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.m1", "Hola, quisiera informes")])
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.m2", "¿Cuánto cuesta la página web?")])
    (pendiente,) = ctx.turnos_pendientes.values()
    assert [m.wa_message_id for m in pendiente.items] == ["wamid.m1", "wamid.m2"]
    assert pendiente.fallas == 2
    crm.caido = False
    assert await _hasta(lambda: routes["messages"].called)
    await asyncio.sleep(0.5)  # ningún reintento viejo sobrevivió para contestar otra vez
    assert routes["messages"].call_count == 1
    assert len(ctx.llm.calls) == 1
    assert _usuario(ctx) == "Hola, quisiera informes\n¿Cuánto cuesta la página web?"
    assert ctx.turnos_pendientes == {}
    await ctx.crm.aclose()


async def test_agotados_los_reintentos_handoff_error_en_cuanto_el_crm_contesta(respx_mock):
    ctx = _ctx("0.05")
    routes = mock_crm_basics(respx_mock)
    crm = CrmIntermitente()
    routes["context"].mock(side_effect=crm)
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.tarde", "hola")])
    assert await _hasta(lambda: any(p.rendida for p in ctx.turnos_pendientes.values()))
    await asyncio.sleep(0.15)  # la vigilia pregunta y el CRM sigue caído
    assert routes["handoff"].call_count == 0
    crm.caido = False
    assert await _hasta(lambda: routes["handoff"].called)
    assert json.loads(routes["handoff"].calls[0].request.content)["reason"] == "error"
    assert routes["messages"].call_count == 0  # nada fuera de tiempo al lead
    assert ctx.turnos_pendientes == {}
    await ctx.crm.aclose()


async def test_la_vigilia_no_pisa_a_una_persona_que_ya_la_tomo(respx_mock):
    ctx = _ctx("")  # sin reintentos: directo a la vigilia
    routes = mock_crm_basics(respx_mock)
    llamadas = {"n": 0}

    def contexto(request):
        llamadas["n"] += 1
        if llamadas["n"] <= turn.CONTEXT_ATTEMPTS:
            raise httpx.ConnectError("caído", request=request)
        return httpx.Response(200, json=crm_context(ai_enabled=False))

    routes["context"].mock(side_effect=contexto)
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.humano", "hola")])
    assert await _hasta(lambda: not ctx.turnos_pendientes)
    assert routes["handoff"].call_count == 0
    await ctx.crm.aclose()


async def test_tras_rendirse_el_mensaje_nuevo_se_lleva_la_rafaga_vieja(respx_mock):
    ctx = _ctx("0.05")
    routes = mock_crm_basics(respx_mock)
    crm = CrmIntermitente()
    routes["context"].mock(side_effect=crm)
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.v1", "¿tienen citas?")])
    assert await _hasta(lambda: any(p.rendida for p in ctx.turnos_pendientes.values()))
    (vigilia,) = [p.tarea for p in ctx.turnos_pendientes.values()]
    crm.caido = False
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.v2", "¿hola?")])
    assert routes["messages"].call_count == 1
    assert _usuario(ctx) == "¿tienen citas?\n¿hola?"
    await asyncio.sleep(0.15)
    assert vigilia.cancelled()
    assert routes["handoff"].call_count == 0
    await ctx.crm.aclose()


async def test_apagar_cancela_lo_que_esperaba_al_crm(respx_mock):
    ctx = _ctx("30")
    routes = mock_crm_basics(respx_mock)
    routes["context"].mock(side_effect=CrmIntermitente())
    await turn.handle_flush(ctx, IDENTITY, [_msg("wamid.off", "hola")])
    (tarea,) = [p.tarea for p in ctx.turnos_pendientes.values()]
    await turn.cancelar_pendientes(ctx)
    assert tarea.cancelled()
    assert ctx.turnos_pendientes == {}
    await ctx.crm.aclose()


def test_la_rafaga_pendiente_es_de_una_organizacion():
    ctx = make_ctx()
    otra = replace(ctx, organizacion=("org_b", "b"))
    assert turn._clave(ctx, IDENTITY) != turn._clave(otra, IDENTITY)
    assert otra.turnos_pendientes is ctx.turnos_pendientes  # un solo registro


@pytest.mark.parametrize(
    ("valor", "esperas"),
    [
        (None, (15.0, 45.0, 120.0, 300.0)),
        ("5, x, 0, -1, 10", (5.0, 10.0)),
        ("300,300,300", (300.0, 300.0)),  # la suma no pasa de 10 min
        ("", ()),
    ],
)
def test_turn_retry_delays(valor, esperas):
    settings = make_settings() if valor is None else make_settings(turn_retry_delays=valor)
    assert settings.turn_retry_schedule == esperas

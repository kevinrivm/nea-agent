"""propose_slots consulta una fecha solo si alguien nombró un día.

Fija el fallo de tienda-bachi (16 oct 2026): un viernes a la 1:39 pm el lead
preguntó «¿cuándo podría ser?» y el modelo consultó lunes, martes y miércoles
con fecha. Nunca pidió el reparto, que empieza hoy, y el viernes libre jamás
se ofreció.
"""
from __future__ import annotations

import httpx
import pytest

from app.dias import nombra_un_dia
from app.state import BotMessage
from app.tools import ToolRuntime
from app.turn import _conversacion_reciente
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

AVAILABILITY = f"{CRM_URL}/api/bot/availability"

REPARTO = {
    "slots": [
        {
            "startUtc": "2026-10-16T21:00:00.000Z",
            "label": "vie 16 oct, 16:00",
            "dayLabel": "hoy viernes 16 de octubre",
            "time": "16:00",
        },
        {
            "startUtc": "2026-10-19T14:00:00.000Z",
            "label": "lun 19 oct, 09:00",
            "dayLabel": "lunes 19 de octubre",
            "time": "09:00",
        },
    ],
    "query": {"date": None, "status": None, "coveredUntil": "2026-10-21", "horizonEnd": "2026-10-23", "perDay": 3},
}
DIA = {
    "slots": [
        {
            "startUtc": "2026-10-19T14:00:00.000Z",
            "label": "lun 19 oct, 09:00",
            "dayLabel": "lunes 19 de octubre",
            "time": "09:00",
        }
    ],
    "query": {"date": "2026-10-19", "status": "available", "coveredUntil": "2026-10-19", "horizonEnd": "2026-10-23", "perDay": None},
}


def _crm(respx_mock):
    def responder(request: httpx.Request) -> httpx.Response:
        con_fecha = request.url.params.get("date")
        return httpx.Response(200, json=DIA if con_fecha else REPARTO)

    return respx_mock.get(AVAILABILITY).mock(side_effect=responder)


async def _runtime(reciente: str | None):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    return ToolRuntime(ctx, conv, CRM_CONV_ID, conversacion_reciente=reciente), ctx


@pytest.mark.parametrize(
    "texto",
    [
        "quisiera agendar una reunion, cuando podría ser?",
        "¿qué horarios tienes?",
        "sí, agendemos",
        "¿tienes algo en la mañana?",
        "prefiero por las mañanas",
        "a las 4 me queda bien",
    ],
)
def test_no_nombra_un_dia(texto):
    assert not nombra_un_dia(texto)


@pytest.mark.parametrize(
    "texto",
    [
        "¿el jueves?",
        "el miercoles mejor",
        "MIÉRCOLES",
        "hoy no tienes tiempo?",
        "mañana puedo",
        "¿puede ser de mañana?",
        "pasado mañana",
        "la próxima semana",
        "el 20",
        "20 de octubre",
        "20/10",
        "¿tienes otro día?",
        "más adelante",
        "el fin de semana",
    ],
)
def test_nombra_un_dia(texto):
    assert nombra_un_dia(texto)


async def test_sin_dia_nombrado_la_fecha_se_ignora_y_va_el_reparto(respx_mock):
    route = _crm(respx_mock)
    runtime, ctx = await _runtime("quisiera agendar una reunion, cuando podría ser?")
    result = await runtime.execute("propose_slots", {"fecha": "2026-10-19"})
    assert "date" not in route.calls[0].request.url.params
    assert result["ok"] is True
    assert result["fecha_no_consultada"] == "2026-10-19"
    assert result["instrucciones"].startswith("No consulté el 2026-10-19")
    assert any(s["label"].startswith("hoy viernes") for s in result["slots"])
    await ctx.crm.aclose()


async def test_con_dia_nombrado_la_fecha_pasa(respx_mock):
    route = _crm(respx_mock)
    runtime, ctx = await _runtime("¿y el lunes?")
    result = await runtime.execute("propose_slots", {"fecha": "2026-10-19"})
    assert route.calls[0].request.url.params["date"] == "2026-10-19"
    assert result["fecha"] == "2026-10-19"
    assert "fecha_no_consultada" not in result
    await ctx.crm.aclose()


async def test_sin_conversacion_conocida_la_fecha_pasa(respx_mock):
    """Quien no pasa la conversación conserva el comportamiento de antes."""
    route = _crm(respx_mock)
    runtime, ctx = await _runtime(None)
    await runtime.execute("propose_slots", {"fecha": "2026-10-19"})
    assert route.calls[0].request.url.params["date"] == "2026-10-19"
    await ctx.crm.aclose()


def test_el_dia_que_propuso_nea_cuenta_como_pedido():
    history = [
        BotMessage(id=1, conversation_id=1, role="user", content="quiero agendar"),
        BotMessage(id=2, conversation_id=1, role="assistant", content="¿Te late el jueves?"),
        BotMessage(id=3, conversation_id=1, role="user", content="sí"),
    ]
    assert nombra_un_dia(_conversacion_reciente(history))


def test_solo_cuenta_lo_reciente():
    history = [
        BotMessage(id=1, conversation_id=1, role="user", content="el lunes no puedo"),
        BotMessage(id=2, conversation_id=1, role="assistant", content="Va."),
        BotMessage(id=3, conversation_id=1, role="user", content="ok"),
        BotMessage(id=4, conversation_id=1, role="user", content="gracias"),
        BotMessage(id=5, conversation_id=1, role="user", content="¿cuándo se puede?"),
    ]
    assert not nombra_un_dia(_conversacion_reciente(history))

"""Compatibilidad con el motor de agenda de Vocero.

Vocero bajó a su núcleo la garantía "solo se reserva lo que se ofreció": la
oferta se registra CONTRA UNA CONVERSACIÓN y él decide qué es reservable. Estos
tests fijan que Nea se acopla a eso — y cubren los puntos donde antes rompía en
silencio contra un Vocero actual.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.crm import AgendaUnavailable, CrmClient
from app.state import OfferedSlot
from app.tools import AGENDA_TOOLS, ToolRuntime, tool_schemas
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

SLOT_ISO = "2026-07-20T16:00:00Z"
SLOT_DT = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
async def runtime_y_ctx():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.replace_offered_slots(
        conv.id,
        [
            OfferedSlot(
                conversation_id=conv.id,
                start_utc=SLOT_DT,
                end_utc=None,
                label="lunes 20 de julio, 10:00 am",
            )
        ],
    )
    yield ToolRuntime(ctx, conv, CRM_CONV_ID), ctx, conv
    await ctx.crm.aclose()


async def test_propose_slots_manda_la_conversacion(runtime_y_ctx, respx_mock):
    """Sin `conversationId` el CRM responde 422 y Nea se quedaba sin poder
    ofrecer NADA: es la incompatibilidad que rompía el agendamiento entero."""
    runtime, _ctx, _conv = runtime_y_ctx
    route = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": [
                    {
                        "startUtc": SLOT_ISO,
                        "endUtc": "2026-07-20T16:30:00Z",
                        "label": "lun 20 jul, 10:00",
                        "dayLabel": "lunes 20 de julio",
                        "time": "10:00",
                    }
                ]
            },
        )
    )
    result = await runtime.execute("propose_slots", {})
    assert result["ok"] is True
    assert route.calls[0].request.url.params["conversationId"] == CRM_CONV_ID


async def test_slot_no_ofrecido_resincroniza_con_lo_que_dice_el_crm(
    runtime_y_ctx, respx_mock
):
    """El CRM manda: si dice que ese horario no está ofrecido, su lista gana y
    el agente re-ofrece esa. Antes esto caía en el error genérico y el lead
    solo escuchaba que no se pudo."""
    runtime, ctx, conv = runtime_y_ctx
    del_crm = [
        {
            "startUtc": "2026-07-21T17:00:00Z",
            "label": "mar 21 jul, 11:00",
            "dayLabel": "martes 21 de julio",
            "time": "11:00",
        }
    ]
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {"code": "slot_not_offered", "message": "no se ofrecio"},
                "slots": del_crm,
            },
        )
    )
    result = await runtime.execute(
        "book_session", {"start_utc": SLOT_ISO, "dia_confirmado": "el lunes"}
    )
    assert result["ok"] is False
    assert result["error"] == "slot_no_ofrecido"
    # Re-ofrece lo del CRM, no se queda mudo.
    assert result["slots"][0]["label"] == "martes 21 de julio, 11:00"
    # Y el espejo local quedó igual al del CRM.
    espejo = await ctx.store.get_offered_slots(conv.id)
    assert [s.start_utc for s in espejo] == [
        datetime(2026, 7, 21, 17, 0, tzinfo=timezone.utc)
    ]
    assert runtime.booked is False


async def test_slot_no_ofrecido_sin_alternativas_manda_a_re_ofrecer(
    runtime_y_ctx, respx_mock
):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            409, json={"error": {"code": "slot_not_offered"}, "slots": []}
        )
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["ok"] is False
    assert "propose_slots" in result["detalle"]
    assert await ctx.store.get_offered_slots(conv.id) == []


async def test_agenda_apagada_deja_de_prometer_citas(runtime_y_ctx, respx_mock):
    """Vocero trae el motor detrás de una bandera y viene APAGADO por defecto:
    esos endpoints responden 404. Reintentar contra una puerta que no existe le
    daría evasivas al lead en vez de un handoff limpio."""
    runtime, ctx, _conv = runtime_y_ctx
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(404)
    )
    result = await runtime.execute("propose_slots", {})
    assert result["ok"] is False
    assert result["error"] == "sin_agenda"
    # Y no se vuelve a intentar en los siguientes turnos.
    assert ctx.agenda_enabled is False


async def test_sin_agenda_no_se_le_ensenan_las_herramientas_de_agendar():
    """Que la herramienta no exista es más claro que pedirle al prompt que se
    acuerde de no usarla."""
    con = {t["function"]["name"] for t in tool_schemas(True)}
    sin = {t["function"]["name"] for t in tool_schemas(False)}
    assert AGENDA_TOOLS <= con
    assert not (AGENDA_TOOLS & sin)
    # Lo demás sigue disponible: sin agenda el agente igual califica y escala.
    assert {"update_ficha", "handoff"} <= sin


async def test_el_enlace_de_la_reunion_llega_aunque_no_sea_zoom(
    runtime_y_ctx, respx_mock
):
    """Vocero devuelve `meetingLink` desde que la reunión la entrega un
    conector (Zoom, Meet o la sala fija). Leer solo `zoomJoinUrl` dejaba al
    lead con la cita creada y sin por dónde entrar."""
    runtime, _ctx, _conv = runtime_y_ctx
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={
                "bookingId": "bk_1",
                "meetingLink": "https://meet.google.com/abc",
                "linkPending": False,
                "label": "lun 20 jul, 10:00",
            },
        )
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["ok"] is True
    assert result["meeting_url"] == "https://meet.google.com/abc"
    assert result["enlace_pendiente"] is False


async def test_enlace_pendiente_no_se_promete(runtime_y_ctx, respx_mock):
    """El proveedor falló: la cita SÍ existe, el enlace todavía no. Confirmar
    la cita sin prometer un enlace que no se tiene."""
    runtime, _ctx, _conv = runtime_y_ctx
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={"bookingId": "bk_1", "meetingLink": None, "linkPending": True},
        )
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["ok"] is True
    assert result["meeting_url"] is None
    assert result["enlace_pendiente"] is True
    assert "no prometas" in result["instrucciones"]


async def test_zoom_join_url_sigue_sirviendo(runtime_y_ctx, respx_mock):
    """Compatibilidad hacia atrás: un CRM viejo manda `zoomJoinUrl`."""
    runtime, _ctx, _conv = runtime_y_ctx
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201, json={"bookingId": "bk_1", "zoomJoinUrl": "https://zoom.us/j/1"}
        )
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["meeting_url"] == "https://zoom.us/j/1"


@pytest.mark.parametrize(
    "status, esperado",
    [
        # Con agenda encendida el CRM pide la conversación: 422 = sí hay agenda.
        (422, True),
        (404, False),
        (200, True),
    ],
)
async def test_sonda_de_capacidad(respx_mock, status, esperado):
    """Se pregunta SIN conversationId a propósito: distingue las dos
    configuraciones sin ensuciar la oferta de nadie ni pedirle al CRM un
    endpoint nuevo."""
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(status, json={})
    )
    crm = CrmClient(CRM_URL, "k")
    try:
        assert await crm.agenda_available() is esperado
    finally:
        await crm.aclose()


async def test_sonda_ante_crm_caido_asume_que_si_hay_agenda(respx_mock):
    """Equivocarse hacia el sí cuesta un intento fallido que ya degrada solo;
    hacia el no apagaría el agendamiento de una instancia que sí lo tiene."""
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        side_effect=httpx.ConnectError("sin red")
    )
    crm = CrmClient(CRM_URL, "k")
    try:
        assert await crm.agenda_available() is True
    finally:
        await crm.aclose()


async def test_availability_404_es_agenda_apagada_no_error_generico(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(404)
    )
    crm = CrmClient(CRM_URL, "k")
    try:
        with pytest.raises(AgendaUnavailable):
            await crm.get_availability(CRM_CONV_ID)
    finally:
        await crm.aclose()

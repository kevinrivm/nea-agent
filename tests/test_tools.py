"""Tools: book_session SOLO acepta slots ofrecidos; slot_taken trae alternativas."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.state import OfferedSlot
from app.tools import ToolRuntime
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
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID)
    yield runtime, ctx, conv
    await ctx.crm.aclose()


async def test_book_rechaza_slot_no_ofrecido(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    bookings = respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(200, json={"bookingId": "bk_1", "label": "x"})
    )
    result = await runtime.execute(
        "book_session", {"start_utc": "2026-07-20T17:00:00Z"}  # nunca ofrecido
    )
    assert result["ok"] is False
    assert result["error"] == "slot_no_ofrecido"
    assert bookings.call_count == 0  # jamás llegó al CRM
    assert runtime.booked is False


async def test_book_acepta_slot_ofrecido_epoch_exacto(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    bookings = respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        # 201 Created: el código REAL del CRM (route.ts responde 201, no 200 —
        # el mock infiel escondió este bug hasta la certificación 002).
        return_value=httpx.Response(
            201,
            json={
                "bookingId": "bk_1",
                "zoomJoinUrl": "https://zoom.us/j/1",
                "label": "lunes 20 de julio, 10:00 am",
            },
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}, "stageMoved": True})
    )
    # mismo instante escrito con offset en vez de Z — el epoch es lo que cuenta
    result = await runtime.execute(
        "book_session", {"start_utc": "2026-07-20T16:00:00+00:00"}
    )
    assert result["ok"] is True
    assert runtime.booked is True
    body = json.loads(bookings.calls[0].request.content)
    assert body == {"conversationId": CRM_CONV_ID, "startUtc": SLOT_ISO}
    # al reservar se limpian los ofrecidos
    assert await ctx.store.get_offered_slots(conv.id) == []


async def test_booking_confirmation_includes_authoritative_meeting_link(runtime_y_ctx):
    runtime, _, _ = runtime_y_ctx
    runtime.booking_confirmation = {"label":"viernes 18 de septiembre, 10:00 am","meeting_url":"https://zoom.us/j/123","link_pending":False,"reminder_consent":True}
    reply=runtime.finalize_reply("Te mandaré el enlace después")
    assert "Enlace de la reunión: https://zoom.us/j/123" in reply
    assert "después" not in reply
    assert "recordatorios" in reply


@pytest.mark.parametrize(
    "enlace",
    ["https://meet.google.com/abc-defg-hij", "https://sala.negocio.test/fija", "https://zoom.us/j/1"],
)
async def test_la_confirmacion_no_le_pone_proveedor_al_enlace(runtime_y_ctx, enlace):
    """Ningún CRM dice de qué proveedor es el enlace (Zoom, Meet o la sala
    fija del negocio): llamarlo "de Zoom" le mentía a quien recibió un Meet."""
    runtime, _, _ = runtime_y_ctx
    runtime.booking_confirmation = {"label": "lunes 20 de julio, 10:00 am", "meeting_url": enlace, "link_pending": False, "reminder_consent": False}
    reply = runtime.finalize_reply("texto del modelo")
    assert f"Enlace de la reunión: {enlace}" in reply
    assert "Zoom" not in reply and "Meet" not in reply
    assert "recordatorios" not in reply


async def test_reservar_con_un_meet_confirma_con_el_enlace_neutro(runtime_y_ctx, respx_mock):
    """La cadena real: lo que devuelve el CRM → lo que lee el lead."""
    runtime, _, _ = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={"bookingId": "bk_1", "meetingLink": "https://meet.google.com/abc", "linkPending": False, "label": "lun 20 jul, 10:00"},
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(return_value=httpx.Response(200, json={}))
    result = await runtime.execute(
        "book_session",
        {"start_utc": SLOT_ISO, "dia_confirmado": "sí, el lunes", "recordatorios_aceptados": False},
    )
    assert result["ok"] is True
    assert runtime.finalize_reply("¡Listo!") == (
        "Listo, tu cita quedó confirmada para lunes 20 de julio, 10:00 am.\n\n"
        "Enlace de la reunión: https://meet.google.com/abc"
    )


async def test_la_confirmacion_con_enlace_pendiente_no_promete_uno(runtime_y_ctx):
    runtime, _, _ = runtime_y_ctx
    runtime.booking_confirmation = {"label": "lunes 20 de julio, 10:00 am", "meeting_url": None, "link_pending": True, "reminder_consent": False}
    reply = runtime.finalize_reply("texto del modelo")
    assert "Enlace de" not in reply
    assert "te llegará por aquí en un momento" in reply


async def test_book_slot_taken_ofrece_alternativas_frescas(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    frescos = [
        {"startUtc": "2026-07-21T16:00:00Z", "endUtc": None, "label": "martes 21, 10:00 am"},
        {"startUtc": "2026-07-21T17:00:00Z", "endUtc": None, "label": "martes 21, 11:00 am"},
    ]
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        # Forma REAL del CRM: el código va anidado bajo "error". El mock plano
        # de antes escondía que en producción ningún 409 se reconocía y este
        # camino estaba muerto.
        return_value=httpx.Response(
            409,
            json={
                "error": {"code": "slot_taken", "message": "ocupado"},
                "slots": frescos,
            },
        )
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["ok"] is False
    assert result["error"] == "slot_taken"
    assert [s["label"] for s in result["slots"]] == [s["label"] for s in frescos]
    # los frescos quedan como los nuevos (y únicos) reservables
    offered = await ctx.store.get_offered_slots(conv.id)
    assert [s.label for s in offered] == [s["label"] for s in frescos]
    assert runtime.booked is False


async def test_propose_slots_pide_reparto_por_dia_y_persiste_todos(
    runtime_y_ctx, respx_mock
):
    """El catálogo reservable es ancho a propósito: guardar solo 3 dejaba al
    agente sin nada que ofrecer cuando el lead pedía otro día. El "máximo 3"
    es cuántos SE ENSEÑAN, y eso lo gobierna el prompt."""
    runtime, ctx, conv = runtime_y_ctx
    seis = [
        {
            "startUtc": f"2026-07-2{d}T16:00:00Z",
            "endUtc": f"2026-07-2{d}T16:30:00Z",
            "label": f"día 2{d}, 10:00 am",
        }
        for d in range(6)
    ]
    route = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(200, json={"slots": seis})
    )
    result = await runtime.execute("propose_slots", {})
    assert result["ok"] is True
    assert len(result["slots"]) == 6
    assert len(await ctx.store.get_offered_slots(conv.id)) == 6
    assert runtime.proposed is True
    # El reparto se le pide al CRM, no se improvisa aquí.
    params = route.calls[0].request.url.params
    assert params["perDay"] == "3"
    assert params["days"] == "5"


async def test_update_ficha_manda_lo_que_haya(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    ficha_route = respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}, "stageMoved": False})
    )
    result = await runtime.execute(
        "update_ficha",
        {"rubro": "clínica dental", "rol": "el dueño mero mero", "campo_raro": "x"},
    )
    assert result["ok"] is True
    body = json.loads(ficha_route.calls[0].request.content)
    # drift tolerado: se manda tal cual, el CRM normaliza flojo
    assert body["ficha"]["rol"] == "el dueño mero mero"
    assert body["ficha"]["campo_raro"] == "x"


async def test_handoff_se_difiere_al_final_del_turno(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    handoff_route = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await runtime.execute("handoff", {"reason": "pidió humano"})
    assert result["ok"] is True
    assert runtime.handoff_reason == "pidió humano"
    # la tool NO llama al CRM: turn.py lo hace después de la despedida
    assert handoff_route.call_count == 0


async def test_crm_caido_en_tool_no_tumba_el_turno(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(500)
    )
    result = await runtime.execute("update_ficha", {"rubro": "ferretería"})
    assert result["ok"] is False
    assert result["error"] == "crm_error"


async def test_propose_slots_etiqueta_con_el_dia_en_palabras(
    runtime_y_ctx, respx_mock
):
    """La etiqueta corta ("vie 7 ago, 10:30") se presta a que el lead entienda
    otro día — basta un "10:30, de mañana" para agendar mal."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": [
                    {
                        "startUtc": "2026-08-07T16:30:00Z",
                        "endUtc": "2026-08-07T17:00:00Z",
                        "label": "vie 7 ago, 10:30",
                        "dayLabel": "hoy viernes 7 de agosto",
                        "time": "10:30",
                    },
                    {
                        "startUtc": "2026-08-10T15:00:00Z",
                        "endUtc": "2026-08-10T15:30:00Z",
                        "label": "lun 10 ago, 09:00",
                        "dayLabel": "lunes 10 de agosto",
                        "time": "09:00",
                    },
                ]
            },
        )
    )
    result = await runtime.execute("propose_slots", {})
    assert [s["label"] for s in result["slots"]] == [
        "hoy viernes 7 de agosto, 10:30",
        "lunes 10 de agosto, 09:00",
    ]
    assert result["dias_con_agenda"] == [
        "hoy viernes 7 de agosto",
        "lunes 10 de agosto",
    ]


async def test_reschedule_mueve_la_cita_sin_handoff(runtime_y_ctx, respx_mock):
    """Antes esto era handoff obligado y el lead se quedaba sin nadie."""
    runtime, ctx, conv = runtime_y_ctx
    patch = respx_mock.patch(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            200, json={"bookingId": "bk_1", "zoomJoinUrl": "https://meet.test/1"}
        )
    )
    result = await runtime.execute(
        "reschedule_session",
        {"start_utc": SLOT_ISO, "dia_confirmado": "sí, el lunes 20"},
    )
    assert result["ok"] is True
    assert result["label"] == "lunes 20 de julio, 10:00 am"
    assert json.loads(patch.calls[0].request.content) == {
        "conversationId": CRM_CONV_ID,
        "startUtc": SLOT_ISO,
    }
    assert await ctx.store.get_offered_slots(conv.id) == []


async def test_reschedule_rechaza_slot_no_ofrecido(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    patch = respx_mock.patch(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await runtime.execute(
        "reschedule_session",
        {"start_utc": "2026-07-20T17:00:00Z", "dia_confirmado": "el lunes"},
    )
    assert result["error"] == "slot_no_ofrecido"
    assert patch.call_count == 0


async def test_reschedule_sin_cita_manda_a_book(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.patch(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "no_booking", "message": "sin cita"}}
        )
    )
    result = await runtime.execute(
        "reschedule_session", {"start_utc": SLOT_ISO, "dia_confirmado": "el lunes"}
    )
    assert result["ok"] is False
    assert result["error"] == "sin_cita"

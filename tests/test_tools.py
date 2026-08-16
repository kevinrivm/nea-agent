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


async def test_book_slot_taken_ofrece_alternativas_frescas(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    frescos = [
        {"startUtc": "2026-07-21T16:00:00Z", "endUtc": None, "label": "martes 21, 10:00 am"},
        {"startUtc": "2026-07-21T17:00:00Z", "endUtc": None, "label": "martes 21, 11:00 am"},
    ]
    # Forma REAL del CRM: el código va ANIDADO bajo `error` (lib/api.ts).
    # El mock viejo lo ponía en la raíz y por eso este test pasaba mientras en
    # producción el slot_taken nunca se activaba.
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {"code": "slot_taken", "message": "ese horario ya se ocupó"},
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


async def test_book_slot_taken_tambien_con_el_codigo_en_la_raiz(
    runtime_y_ctx, respx_mock
):
    """Compatibilidad: un CRM que emita el código plano sigue funcionando."""
    runtime, ctx, conv = runtime_y_ctx
    frescos = [
        {"startUtc": "2026-07-21T16:00:00Z", "endUtc": None, "label": "martes 21, 10:00 am"},
    ]
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(409, json={"code": "slot_taken", "slots": frescos})
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["ok"] is False
    assert result["error"] == "slot_taken"
    assert [s["label"] for s in result["slots"]] == [s["label"] for s in frescos]


async def test_book_devuelve_join_url_del_contrato(runtime_y_ctx, respx_mock):
    """El agente no se casa con un proveedor: `joinUrl` es el nombre del contrato."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={
                "bookingId": "bk_1",
                "label": "martes 21, 10:00 am",
                "joinUrl": "https://reunion.ejemplo.com/abc",
            },
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}})
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["ok"] is True
    assert result["zoom_url"] == "https://reunion.ejemplo.com/abc"


async def test_propose_slots_maximo_3_y_persistidos(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    seis = [
        {
            "startUtc": f"2026-07-2{d}T16:00:00Z",
            "endUtc": f"2026-07-2{d}T16:30:00Z",
            "label": f"día 2{d}, 10:00 am",
        }
        for d in range(6)
    ]
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(200, json={"slots": seis})
    )
    result = await runtime.execute("propose_slots", {})
    assert result["ok"] is True
    assert len(result["slots"]) == 3  # máx 3 por mensaje
    offered = await ctx.store.get_offered_slots(conv.id)
    assert len(offered) == 3
    assert runtime.proposed is True


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

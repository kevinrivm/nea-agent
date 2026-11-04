"""Solo el 404 VACÍO es la agenda apagada.

Vocero contesta 404 de dos maneras en su superficie de agenda: vacío cuando la
bandera `AGENDA` está apagada (el endpoint no existe en esa instancia) y con su
sobre de error cuando, con la agenda encendida, no encuentra la conversación o
la cita. Nea las leía igual, y eso costaba dos cosas:

- la sonda de cloud pregunta por una conversación inventada (`cv_sonda`): con
  la agenda ENCENDIDA recibía el 404 con sobre y concluía «apagada», así que
  una Nea cloud de un solo negocio arrancaba siempre sin agenda;
- una herramienta que pedía huecos o reservaba para una conversación que el
  CRM no encontraba apagaba la agenda de todo el proceso.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.crm import AgendaUnavailable, CrmClient, CrmError
from app.crm_brains import BrainsCrmClient
from app.state import OfferedSlot
from app.tools import ToolRuntime
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

SLOT_ISO = "2026-07-20T16:00:00Z"
SLOT_DT = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)
SOBRE_404 = {"error": {"code": "not_found", "message": "Conversación no encontrada"}}


# ── La sonda ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "respuesta, esperado",
    [
        (httpx.Response(404), False),  # la bandera: vacío
        (httpx.Response(404, text="<html>404</html>"), False),  # CRM viejo sin la ruta
        (httpx.Response(404, json=SOBRE_404), True),  # el handler existe
        (httpx.Response(422, json={"error": {"code": "invalid_body"}}), True),
        (httpx.Response(401), True),
        (httpx.Response(500), None),
        (httpx.Response(503), None),
    ],
)
async def test_la_sonda_de_siempre_distingue_los_404(respx_mock, respuesta, esperado):
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(return_value=respuesta)
    crm = CrmClient(CRM_URL, "k")
    try:
        assert await crm.sondear_agenda() is esperado
        # La pregunta de siempre resuelve la duda hacia el sí.
        assert await crm.agenda_available() is (True if esperado is None else esperado)
    finally:
        await crm.aclose()


async def test_la_sonda_sin_red_no_concluye(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        side_effect=httpx.ConnectError("sin red")
    )
    crm = CrmClient(CRM_URL, "k")
    try:
        assert await crm.sondear_agenda() is None
        assert await crm.agenda_available() is True
    finally:
        await crm.aclose()


async def test_el_timeout_de_la_sonda_viaja_en_la_peticion(respx_mock):
    ruta = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(422, json={})
    )
    crm = CrmClient(CRM_URL, "k")
    try:
        await crm.sondear_agenda(timeout=2.5)
    finally:
        await crm.aclose()
    assert ruta.calls[0].request.extensions["timeout"]["read"] == 2.5


async def test_la_sonda_de_cloud_con_agenda_encendida_dice_que_si(respx_mock):
    """EL fallo: con AGENDA=on, `cv_sonda` no existe y el CRM responde 404 con
    su sobre. Leído como «apagada», una Nea cloud de un negocio arrancaba
    siempre sin agenda."""
    ruta = respx_mock.get(f"{CRM_URL}/api/brains/agenda/slots").mock(
        return_value=httpx.Response(404, json=SOBRE_404)
    )
    crm = BrainsCrmClient(CRM_URL, "secreto", "negocio-a")
    try:
        assert await crm.sondear_agenda() is True
        assert await crm.agenda_available() is True
    finally:
        await crm.aclose()
    assert ruta.calls[0].request.url.params["conversationId"] == "cv_sonda"


async def test_la_sonda_de_cloud_con_la_bandera_apagada_dice_que_no(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/brains/agenda/slots").mock(
        return_value=httpx.Response(404)
    )
    crm = BrainsCrmClient(CRM_URL, "secreto", "negocio-a")
    try:
        assert await crm.sondear_agenda() is False
    finally:
        await crm.aclose()


# ── Las herramientas ──────────────────────────────────────────────────────


@pytest.fixture
async def runtime_y_ctx():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.replace_offered_slots(
        conv.id, [OfferedSlot(conv.id, SLOT_DT, None, "lunes 20 de julio, 10:00 am")]
    )
    yield ToolRuntime(ctx, conv, CRM_CONV_ID), ctx
    await ctx.crm.aclose()


async def test_huecos_de_una_conversacion_que_no_existe_no_apagan_la_agenda(
    runtime_y_ctx, respx_mock
):
    runtime, ctx = runtime_y_ctx
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(404, json=SOBRE_404)
    )
    result = await runtime.execute("propose_slots", {})
    assert result["error"] == "crm_error"
    assert ctx.agenda_enabled is True


async def test_reservar_en_una_conversacion_que_no_existe_no_apaga_la_agenda(
    runtime_y_ctx, respx_mock
):
    runtime, ctx = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(404, json={**SOBRE_404, "slots": []})
    )
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["error"] == "crm_error"
    assert ctx.agenda_enabled is True


async def test_el_404_vacio_sigue_siendo_agenda_apagada(runtime_y_ctx, respx_mock):
    runtime, ctx = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(return_value=httpx.Response(404))
    result = await runtime.execute("book_session", {"start_utc": SLOT_ISO})
    assert result["error"] == "sin_agenda"
    assert ctx.agenda_enabled is False


async def test_en_cloud_sin_la_conversacion_no_es_agenda_apagada(respx_mock):
    respx_mock.post(f"{CRM_URL}/api/brains/agenda/book").mock(
        return_value=httpx.Response(
            404, json={"ok": False, "code": "not_found", "message": "x", "slots": []}
        )
    )
    respx_mock.get(f"{CRM_URL}/api/brains/agenda/slots").mock(
        return_value=httpx.Response(404, json=SOBRE_404)
    )
    crm = BrainsCrmClient(CRM_URL, "secreto", "negocio-a")
    try:
        with pytest.raises(CrmError) as reserva:
            await crm.create_booking("cv_1", SLOT_ISO)
        assert not isinstance(reserva.value, AgendaUnavailable)
        with pytest.raises(CrmError) as huecos:
            await crm.consultar_huecos("cv_1")
        assert not isinstance(huecos.value, AgendaUnavailable)
        with pytest.raises(CrmError) as reparto:
            await crm.get_availability("cv_1")
        assert not isinstance(reparto.value, AgendaUnavailable)
    finally:
        await crm.aclose()

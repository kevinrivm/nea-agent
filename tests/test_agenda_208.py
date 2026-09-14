import json
import httpx
import pytest
from app.crm import CrmError
from app.crm_brains import BrainsCrmClient
from app.tools import tool_schemas

def test_capabilities_do_not_leak_to_legacy():
    old = tool_schemas(True)
    new = tool_schemas(True, True)
    assert "cancel_session" not in [t["function"]["name"] for t in old]
    assert "cancel_session" in [t["function"]["name"] for t in new]
    assert "cancel_session" not in [t["function"]["name"] for t in tool_schemas(False, True)]
    for t in old:
        if t["function"]["name"] == "reschedule_session":
            assert "selection_token" not in t["function"]["parameters"]["required"]

async def test_envelope_and_same_operation_retry(respx_mock):
    client=BrainsCrmClient("https://crm.test","synthetic","org-a")
    client.registrar_despacho("cv-a","dispatch-a")
    client.registrar_envelope("cv-a",{"dispatchId":"dispatch-a","brainGeneration":7,"capabilities":["agenda_v2"]})
    route=respx_mock.post("https://crm.test/api/brains/agenda/book").mock(return_value=httpx.Response(201,json={"ok":True,"booking":{"id":"booking-a"}}))
    await client.create_booking("cv-a","2026-10-20T12:00:00Z")
    await client.create_booking("cv-a","2026-10-20T12:00:00Z")
    bodies=[json.loads(c.request.content) for c in route.calls]
    assert bodies[0] == bodies[1]
    assert bodies[0]["brainGeneration"] == 7
    assert bodies[0]["dispatchId"] == "dispatch-a"
    assert len(bodies[0]["idempotencyKey"]) == 64
    await client.aclose()

async def test_cancel_keeps_exact_selected_token(respx_mock):
    client=BrainsCrmClient("https://crm.test","synthetic","org-a")
    client.registrar_despacho("cv-a","dispatch-a")
    client.registrar_envelope("cv-a",{"dispatchId":"dispatch-a","brainGeneration":2,"capabilities":["agenda_v2"]})
    route=respx_mock.post("https://crm.test/api/brains/agenda/cancel").mock(return_value=httpx.Response(200,json={"ok":True}))
    await client.cancel_booking("cv-a","selection-of-second-booking",True)
    body=json.loads(route.calls[0].request.content)
    assert body["selectionToken"] == "selection-of-second-booking"
    assert body["confirmation"] is True
    await client.aclose()

async def test_unknown_is_not_empty_availability(respx_mock):
    client=BrainsCrmClient("https://crm.test","synthetic","org-a")
    respx_mock.get("https://crm.test/api/brains/agenda/slots").mock(return_value=httpx.Response(503,json={"error":{"code":"availability_unknown"}}))
    with pytest.raises(CrmError,match="availability_unknown"):
        await client.get_availability("cv-a")
    await client.aclose()

async def test_two_turn_selection_confirmation_and_cancel(respx_mock):
    """Full turn loop with deterministic model; never sends to real WhatsApp."""
    from app.llm import LlmReply, ToolCall
    from app.state import InboundMessage
    from app.turn import run_turn
    from tests.conftest import make_ctx, mock_crm_basics, CRM_CONV_ID, IDENTITY

    ctx=make_ctx()
    routes=mock_crm_basics(respx_mock)
    cloud=BrainsCrmClient("https://crm.test","synthetic","org-a")
    cloud.registrar_despacho(CRM_CONV_ID,"dispatch-confirm")
    cloud.registrar_envelope(CRM_CONV_ID,{"dispatchId":"dispatch-confirm","brainGeneration":3,"capabilities":["agenda_v2"]})
    ctx.crm.supports_agenda_v2=True
    ctx.crm.list_bookings=cloud.list_bookings
    ctx.crm.cancel_booking=cloud.cancel_booking
    selected="opaque-second-booking-selection"
    respx_mock.get("https://crm.test/api/brains/agenda/bookings").mock(return_value=httpx.Response(200,json={"bookings":[{"selectionToken":"opaque-first","label":"martes a las diez","revision":1},{"selectionToken":selected,"label":"jueves a las once","revision":1}]}))
    cancel=respx_mock.post("https://crm.test/api/brains/agenda/cancel").mock(return_value=httpx.Response(200,json={"ok":True}))
    ctx.llm.replies=[LlmReply(content=None,tool_calls=[ToolCall(id="list",name="list_bookings",arguments={})]),LlmReply(content="Tienes martes a las diez y jueves a las once. ¿Cuál quieres cancelar?")]
    await run_turn(ctx,IDENTITY,[InboundMessage(wa_message_id="208-1",identity=IDENTITY,type="text",text="Quiero cancelar una cita")])
    assert cancel.call_count==0
    ctx.llm.replies=[LlmReply(content=None,tool_calls=[ToolCall(id="cancel",name="cancel_session",arguments={"selection_token":selected,"confirmation":True})]),LlmReply(content="Cancelé la cita del jueves a las once.")]
    await run_turn(ctx,IDENTITY,[InboundMessage(wa_message_id="208-2",identity=IDENTITY,type="text",text="Confirmo cancelar la del jueves a las once")])
    assert cancel.call_count==1
    assert json.loads(cancel.calls[0].request.content)["selectionToken"]==selected
    assert routes["messages"].call_count==2
    await cloud.aclose()
    await ctx.crm.aclose()

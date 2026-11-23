"""vocerocrm-cloud 237 (E3): un turno superado por otra ráfaga se calla.

Si el cliente escribe mientras Nea piensa, el CRM ya tiene un despacho nuevo
con TODA la ráfaga. Con la capacidad `dispatch_superado` anunciada, Nea pide
`dropIfSuperseded` al responder; el CRM contesta `409 dispatch_superado` a la
respuesta vieja, y Nea calla sin traspaso y sin guardar esa respuesta. El
despacho siguiente contesta todo junto, una sola vez.

`CUERPO_GEMELO` y `ERROR_GEMELO` están copiados de
`tests/unit/despacho-superado-contrato.test.ts` de vocerocrm-cloud, que fija el
otro lado. Si uno cambia, el otro tiene que cambiar.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import respx

from app.crm import CrmClient, CrmConflict
from app.crm_brains import CAPACIDAD_SUPERADO, BrainsCrmClient
from app.dispatch import _procesar
from app.llm import LlmReply, ToolCall
from tests.conftest import crm_context, make_ctx, make_settings

CRM_URL = "http://crm.test"
CUERPO_GEMELO = (
    '{"dispatchId":"dsp_gemelo1","conversationId":"cv_gemela1",'
    '"text":"¡Hola! Sí tenemos.","dropIfSuperseded":true}'
)
ERROR_GEMELO = (
    '{"error":{"code":"dispatch_superado",'
    '"message":"El cliente escribió de nuevo; responde el despacho más reciente"}}'
)
IDENTIDAD = "5215550002222"
CONV = "cv_sup1"


def test_la_capacidad_es_la_que_anuncia_el_crm():
    assert CAPACIDAD_SUPERADO == "dispatch_superado"


@respx.mock
async def test_con_la_capacidad_manda_el_cuerpo_gemelo():
    ruta = respx.post(f"{CRM_URL}/api/brains/messages").mock(
        return_value=httpx.Response(201, json={"message": {"id": "msg_1"}})
    )
    cliente = BrainsCrmClient(CRM_URL, "secreto", "gemela")
    cliente.registrar_envelope("cv_gemela1", {"dispatchId": "dsp_gemelo1", "capabilities": ["dispatch_envelope_v1", CAPACIDAD_SUPERADO]})
    cliente.registrar_despacho("cv_gemela1", "dsp_gemelo1")
    await cliente.send_message("cv_gemela1", "¡Hola! Sí tenemos.")

    enviado = json.loads(ruta.calls.last.request.content)
    esperado = json.loads(CUERPO_GEMELO)
    assert enviado == esperado
    assert list(enviado) == list(esperado)  # y en el mismo orden
    await cliente.aclose()


@respx.mock
async def test_sin_la_capacidad_el_cuerpo_es_el_de_siempre():
    ruta = respx.post(f"{CRM_URL}/api/brains/messages").mock(
        return_value=httpx.Response(201, json={"message": {"id": "msg_1"}})
    )
    cliente = BrainsCrmClient(CRM_URL, "secreto", "gemela")
    cliente.registrar_envelope("cv_1", {"dispatchId": "dsp_1", "capabilities": ["dispatch_envelope_v1"]})
    cliente.registrar_despacho("cv_1", "dsp_1")
    await cliente.send_message("cv_1", "hola")

    assert "dropIfSuperseded" not in json.loads(ruta.calls.last.request.content)
    await cliente.aclose()


@respx.mock
async def test_el_409_gemelo_llega_como_conflicto_superado():
    respx.post(f"{CRM_URL}/api/brains/messages").mock(
        return_value=httpx.Response(409, content=ERROR_GEMELO, headers={"content-type": "application/json"})
    )
    cliente = BrainsCrmClient(CRM_URL, "secreto", "gemela")
    cliente.registrar_despacho("cv_1", "dsp_1")
    try:
        await cliente.send_message("cv_1", "hola")
    except CrmConflict as exc:
        assert exc.code == "dispatch_superado"
    else:  # pragma: no cover
        raise AssertionError("el 409 debía subir como conflicto")
    await cliente.aclose()


@respx.mock
async def test_la_raiz_no_conoce_el_campo_y_no_lo_recibe():
    """El raíz open source usa esta misma Nea: su `/api/bot/messages` no sabe
    de despachos superados, así que su cuerpo no cambia."""
    ruta = respx.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )
    cliente = CrmClient(CRM_URL, "llave")
    await cliente.send_message("cv_1", "hola")
    assert json.loads(ruta.calls.last.request.content) == {"conversationId": "cv_1", "text": "hola"}
    await cliente.aclose()


# ── El turno ──────────────────────────────────────────────────────────────


def _ctx(monkeypatch):
    ctx = make_ctx(make_settings(vocero_mode="cloud", crm_organization="a", crm_brain_secret="s"))
    ctx.crm.post_typing = AsyncMock()
    ctx.crm.post_handoff = AsyncMock()
    ctx.crm.put_ficha = AsyncMock(return_value={"ficha": {}})
    monkeypatch.setattr("app.turn._fetch_context", AsyncMock(return_value=crm_context(conv_id=CONV)))
    return ctx


def _despacho(dispatch_id: str, *mensajes: tuple[str, str]) -> dict:
    return {
        "dispatchId": dispatch_id,
        "capabilities": ["dispatch_envelope_v1", CAPACIDAD_SUPERADO],
        "organization": {"id": "org_a", "slug": "a"},
        "conversation": {"id": CONV, "channel": "whatsapp"},
        "contact": {"id": "ct_1", "identity": IDENTIDAD, "displayName": "Ana"},
        "messages": [{"id": mid, "type": "text", "text": t} for mid, t in mensajes],
    }


def _historial(ctx, rol: str) -> list[str]:
    return [m.content for m in ctx.store.messages if m.role == rol]


async def test_superado_calla_sin_handoff_y_el_siguiente_contesta_una_vez(monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.crm.send_message = AsyncMock(
        side_effect=[CrmConflict("dispatch_superado"), {"message": {"id": "msg_out"}}]
    )
    enviados = lambda: [c.args[1] for c in ctx.crm.send_message.await_args_list]
    # Aunque el modelo haya pedido traspasar, el turno superado no traspasa:
    # el siguiente decide con TODA la ráfaga.
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="t1", name="handoff", arguments={"reason": "duda"})]),
        LlmReply(content="Sí, tenemos taladros."),
        LlmReply(content="Sí tenemos taladros, y sí enviamos a tu ciudad."),
    ]
    await _procesar(ctx, _despacho("dsp_1", ("msg_1", "¿tienen taladros?")), durable=True)

    assert enviados() == ["Sí, tenemos taladros."]  # el que el CRM rechazó
    ctx.crm.post_handoff.assert_not_awaited()
    assert _historial(ctx, "assistant") == []
    assert _historial(ctx, "user") == ["¿tienen taladros?"]

    # El despacho nuevo repite la ráfaga completa: Nea lee solo lo nuevo y
    # contesta una sola vez, con los dos mensajes a la vista.
    await _procesar(
        ctx,
        _despacho("dsp_2", ("msg_1", "¿tienen taladros?"), ("msg_2", "¿y envían a mi ciudad?")),
        durable=True,
    )
    assert enviados() == ["Sí, tenemos taladros.", "Sí tenemos taladros, y sí enviamos a tu ciudad."]
    assert _historial(ctx, "assistant") == ["Sí tenemos taladros, y sí enviamos a tu ciudad."]
    assert _historial(ctx, "user") == ["¿tienen taladros?", "¿y envían a mi ciudad?"]
    vistos = [m["content"] for m in ctx.llm.calls[-1]["messages"] if m["role"] == "user"]
    assert vistos[-2:] == ["¿tienen taladros?", "¿y envían a mi ciudad?"]
    await ctx.crm.aclose()

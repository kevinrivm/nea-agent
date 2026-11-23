"""vocerocrm-cloud 237 (E4): Nea nunca calla sin rastro.

Antes, si el modelo devolvía un texto vacío o se le acababan las rondas de
herramientas, el turno terminaba sin mandar nada y sin traspasar: el cliente
preguntaba y nadie le contestaba, y en la bandeja no quedaba ninguna señal.
Ahora se le pide UNA corrección al modelo; si vuelve a fallar, el cliente
recibe un acuse corto y la conversación pasa a una persona (`sin_respuesta`).
Solo se calla ante un cierre («gracias», «ok», 👍).

Aquí también: el texto cortado en «:» se corrige, «escribiendo…» se renueva
mientras el turno piensa, y un turno relanzado por la red de seguridad del CRM
se contesta aunque Nea ya hubiera leído sus mensajes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app import turn
from app.cierres import es_cierre, todo_es_cierre
from app.crm_brains import BrainsCrmClient
from app.dispatch import _procesar, es_relanzamiento
from app.llm import LlmReply, ToolCall
from app.turn import ACUSE_TRASPASO, MAX_TOOL_ROUNDS
from tests.conftest import crm_context, make_ctx, make_settings

IDENTIDAD = "5215550003333"
CONV = "cv_calla1"


def _ctx(monkeypatch):
    ctx = make_ctx(make_settings(vocero_mode="cloud", crm_organization="a", crm_brain_secret="s"))
    ctx.crm.post_typing = AsyncMock()
    ctx.crm.post_handoff = AsyncMock()
    ctx.crm.put_ficha = AsyncMock(return_value={"ficha": {}})
    ctx.crm.send_message = AsyncMock(return_value={"message": {"id": "msg_out"}})
    monkeypatch.setattr("app.turn._fetch_context", AsyncMock(return_value=crm_context(conv_id=CONV)))
    return ctx


def _despacho(dispatch_id: str, *mensajes: tuple[str, str], **extra) -> dict:
    return {
        "dispatchId": dispatch_id,
        "organization": {"id": "org_a", "slug": "a"},
        "conversation": {"id": CONV, "channel": "whatsapp"},
        "contact": {"id": "ct_1", "identity": IDENTIDAD, "displayName": "Ana"},
        "messages": [{"id": mid, "type": "text", "text": t} for mid, t in mensajes],
        **extra,
    }


def _enviados(ctx) -> list[str]:
    return [c.args[1] for c in ctx.crm.send_message.await_args_list]


def _sistema_ultimo(ctx) -> str:
    return [m for m in ctx.llm.calls[-1]["messages"] if m["role"] == "system"][-1]["content"]


async def test_vacia_se_corrige_y_sale_la_segunda(monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.llm.replies = [LlmReply(content=""), LlmReply(content="Claro: abrimos de 9 a 18 h.")]
    await _procesar(ctx, _despacho("d1", ("m1", "¿a qué hora abren?")), durable=True)

    assert _enviados(ctx) == ["Claro: abrimos de 9 a 18 h."]
    assert "No le enviaste nada" in _sistema_ultimo(ctx)
    ctx.crm.post_handoff.assert_not_awaited()


async def test_vacia_dos_veces_acuse_y_traspaso_sin_respuesta(monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.llm.replies = [LlmReply(content=""), LlmReply(content="   ")]
    await _procesar(ctx, _despacho("d1", ("m1", "¿cuánto cuesta?")), durable=True)

    assert _enviados(ctx) == [ACUSE_TRASPASO]
    ctx.crm.post_handoff.assert_awaited_once_with(CONV, "sin_respuesta")
    assert [m.content for m in ctx.store.messages if m.role == "assistant"] == [ACUSE_TRASPASO]


async def test_rondas_agotadas_se_corrigen(monkeypatch):
    ctx = _ctx(monkeypatch)
    vuelta = LlmReply(content=None, tool_calls=[ToolCall(id="t", name="no_existe", arguments={})])
    ctx.llm.replies = [vuelta] * MAX_TOOL_ROUNDS + [LlmReply(content="Te cuento: sí hacemos envíos.")]
    await _procesar(ctx, _despacho("d1", ("m1", "¿hacen envíos?")), durable=True)

    assert _enviados(ctx) == ["Te cuento: sí hacemos envíos."]
    assert "rondas de herramientas" in _sistema_ultimo(ctx)


async def test_ante_un_cierre_callar_esta_bien(monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.llm.replies = [LlmReply(content="")]
    await _procesar(ctx, _despacho("d1", ("m1", "muchas gracias 🙏")), durable=True)

    assert _enviados(ctx) == []
    assert len(ctx.llm.calls) == 1  # sin corrección
    ctx.crm.post_handoff.assert_not_awaited()


async def test_un_ok_que_contesta_una_pregunta_no_es_cierre(monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.llm.replies = [LlmReply(content="¿Te agendo el jueves a las 10?")]
    await _procesar(ctx, _despacho("d1", ("m1", "quiero una cita")), durable=True)
    ctx.llm.replies = [LlmReply(content=""), LlmReply(content="¡Listo! Lo dejo apartado.")]
    await _procesar(ctx, _despacho("d2", ("m2", "ok")), durable=True)

    assert _enviados(ctx)[-1] == "¡Listo! Lo dejo apartado."
    assert len(ctx.llm.calls) == 3


async def test_texto_cortado_en_dos_puntos_se_corrige(monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.llm.replies = [
        LlmReply(content="Para agendar necesito tres datos:"),
        LlmReply(content="Para agendar necesito tres datos:\n• nombre\n• día\n• hora"),
    ]
    await _procesar(ctx, _despacho("d1", ("m1", "quiero agendar")), durable=True)

    assert _enviados(ctx) == ["Para agendar necesito tres datos:\n• nombre\n• día\n• hora"]
    assert "«:»" in _sistema_ultimo(ctx) and "COMPLETO" in _sistema_ultimo(ctx)


def test_los_dos_puntos_solo_cuentan_al_final():
    assert turn._respuesta_invalida("Necesitas:\n• correo\n• nombre", []) is None
    assert turn._respuesta_invalida("Agenda aquí: https://ejemplo.test/x", []) is None
    assert turn._respuesta_invalida("Te paso los horarios:", []) is not None


async def test_escribiendo_se_renueva_y_se_para_antes_de_enviar(monkeypatch):
    ctx = _ctx(monkeypatch)
    monkeypatch.setattr(turn, "ESCRIBIENDO_CADA_S", 0.05)
    momentos: list[str] = []
    ctx.crm.post_typing = AsyncMock(side_effect=lambda *a: momentos.append("typing"))

    async def lento(messages, tools=None):
        await asyncio.sleep(0.3)
        return LlmReply(content="Listo, te ayudo con eso.")

    ctx.llm.complete = lento
    ctx.crm.send_message = AsyncMock(side_effect=lambda *a, **k: momentos.append("envío") or {"message": {}})
    await _procesar(ctx, _despacho("d1", ("m1", "hola, una duda")), durable=True)
    await asyncio.sleep(0.2)

    assert momentos.count("typing") >= 3  # el primero y al menos dos renovaciones
    assert momentos[-1] == "envío"  # nada de «escribiendo…» después del mensaje


async def test_un_relanzamiento_contesta_lo_ya_leido(monkeypatch):
    ctx = _ctx(monkeypatch)
    # El primer turno leyó el mensaje, pero su respuesta no salió (aquí, un
    # envío bloqueado): el mensaje queda leído y sin contestar.
    from app.crm import CrmConflict

    ctx.crm.send_message = AsyncMock(side_effect=CrmConflict("ai_paused"))
    ctx.llm.replies = [LlmReply(content="Sí, abrimos el sábado.")]
    await _procesar(ctx, _despacho("d1", ("m1", "¿abren el sábado?")), durable=True)
    assert [m.role for m in ctx.store.messages] == ["user"]

    ctx.crm.send_message = AsyncMock(return_value={"message": {"id": "msg_out"}})
    # Sin `relaunch`, un despacho que solo repite lo leído no gasta turno.
    await _procesar(ctx, _despacho("d2", ("m1", "¿abren el sábado?")), durable=True)
    assert ctx.crm.send_message.await_count == 0

    ctx.llm.replies = [LlmReply(content="Sí, el sábado abrimos de 10 a 14 h.")]
    await _procesar(ctx, _despacho("d3", ("m1", "¿abren el sábado?"), relaunch=1), durable=True)
    assert _enviados(ctx) == ["Sí, el sábado abrimos de 10 a 14 h."]
    # Y no se guarda dos veces lo que dijo el cliente.
    assert [m.content for m in ctx.store.messages if m.role == "user"] == ["¿abren el sábado?"]


async def test_un_relanzamiento_de_algo_ya_contestado_no_contesta_otra_vez(monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.llm.replies = [LlmReply(content="Sí, abrimos el sábado.")]
    await _procesar(ctx, _despacho("d1", ("m1", "¿abren el sábado?")), durable=True)
    await _procesar(ctx, _despacho("d2", ("m1", "¿abren el sábado?"), relaunch=1), durable=True)
    assert _enviados(ctx) == ["Sí, abrimos el sábado."]


def test_que_cuenta_como_relanzamiento():
    assert es_relanzamiento({"relaunch": 1})
    assert es_relanzamiento({"relaunch": 2})
    assert not es_relanzamiento({})
    assert not es_relanzamiento({"relaunch": 0})
    assert not es_relanzamiento({"relaunch": True})
    assert not es_relanzamiento({"relaunch": "1"})


@respx.mock
async def test_en_cloud_los_motivos_propios_no_se_normalizan():
    ruta = respx.post("http://crm.test/api/brains/handoff").mock(return_value=httpx.Response(200, json={"ok": True}))
    cliente = BrainsCrmClient("http://crm.test", "s", "a")
    import json

    for motivo, esperado in [("sin_respuesta", "sin_respuesta"), ("sin_credencial", "sin_credencial"), ("pidió humano", "cliente")]:
        await cliente.post_handoff("cv_1", motivo)
        assert json.loads(ruta.calls.last.request.content)["reason"] == esperado
    await cliente.aclose()


@pytest.mark.parametrize(
    "texto", ["gracias", "ok", "Ok 👍", "👍", "muchas gracias!", "va, gracias", "okkk", "jajaja", "de acuerdo", "buenas noches"]
)
def test_cierres(texto):
    assert es_cierre("text", texto)


@pytest.mark.parametrize(
    "texto", ["¿y el precio?", "gracias, ¿cuánto cuesta?", "quiero agendar", "sí", "a", "hola", "ok pero el jueves no puedo, mejor el viernes a las 5"]
)
def test_no_son_cierres(texto):
    assert not es_cierre("text", texto)


def test_cierres_por_tipo_y_tras_una_pregunta():
    assert es_cierre("reaction", None) and es_cierre("sticker", None)
    assert not es_cierre("image", None)
    assert todo_es_cierre([("text", "ok"), ("text", "gracias")])
    assert not todo_es_cierre([("text", "ok")], "¿Te agendo el jueves?")
    assert not todo_es_cierre([])

"""Un mensaje del CRM se lee UNA vez, aunque venga en dos despachos.

El CRM entrega la ráfaga completa —todo lo que sigue sin respuesta— cada vez
que despacha. Si el cliente escribe mientras un despacho va en vuelo, ese
mensaje sale en otro, con otro `dispatchId`, que repite los anteriores. El
contrato (`contracts/cerebro.md` de vocerocrm-cloud) pide deduplicar por
`messages[].id`; el `dispatchId` solo cubre los reintentos.

Sin esto Nea contestaba bien, pero veía el primer mensaje dos veces en su
historial: «hola» y luego «hola\\n¿hacen envíos?».
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from app.dispatch import _procesar
from app.turn import marca_de_leido
from tests.conftest import crm_context, make_ctx, make_settings

IDENTIDAD = "5215550001111"
CONV = "cv_dedup1"


def cloud():
    ctx = make_ctx(
        make_settings(
            vocero_mode="cloud", crm_organization="a", crm_brain_secret="secreto-de-prueba"
        )
    )
    ctx.crm.post_typing = AsyncMock()
    ctx.crm.send_message = AsyncMock(return_value={"messageId": "msg_out"})
    return ctx


def despacho(dispatch_id: str, *mensajes: tuple[str, str]) -> dict:
    return {
        "dispatchId": dispatch_id,
        "organization": {"id": "org_a", "slug": "a"},
        "conversation": {"id": CONV, "channel": "whatsapp"},
        "contact": {"id": "ct_1", "identity": IDENTIDAD, "displayName": "Ana"},
        "messages": [{"id": mid, "type": "text", "text": texto} for mid, texto in mensajes],
    }


def contexto(monkeypatch, **kwargs) -> AsyncMock:
    mock = AsyncMock(return_value=crm_context(conv_id=CONV, **kwargs))
    monkeypatch.setattr("app.turn._fetch_context", mock)
    return mock


def del_lead(ctx) -> list[str]:
    return [m.content for m in ctx.store.messages if m.role == "user"]


async def test_el_segundo_despacho_repite_el_primero_y_nea_lo_lee_una_vez(monkeypatch):
    ctx = cloud()
    contexto(monkeypatch)

    await _procesar(ctx, despacho("dsp_1", ("msg_1", "hola")), durable=True)
    # El cliente escribió con el primer despacho en vuelo: el CRM manda otro,
    # con la ráfaga completa.
    await _procesar(
        ctx,
        despacho("dsp_2", ("msg_1", "hola"), ("msg_2", "¿hacen envíos?")),
        durable=True,
    )

    assert del_lead(ctx) == ["hola", "¿hacen envíos?"]
    assert len(ctx.llm.calls) == 2
    ultimo = [m for m in ctx.llm.calls[1]["messages"] if m["role"] == "user"][-1]
    assert ultimo["content"] == "¿hacen envíos?"
    await ctx.crm.aclose()


async def test_un_despacho_sin_nada_nuevo_no_gasta_turno(monkeypatch):
    ctx = cloud()
    pedir = contexto(monkeypatch)

    await _procesar(ctx, despacho("dsp_1", ("msg_1", "hola")), durable=True)
    await _procesar(ctx, despacho("dsp_2", ("msg_1", "hola")), durable=True)

    assert del_lead(ctx) == ["hola"]
    assert len(ctx.llm.calls) == 1
    assert pedir.await_count == 1  # ni siquiera se le pide el contexto al CRM
    assert ctx.crm.send_message.await_count == 1
    await ctx.crm.aclose()


async def test_lo_que_un_turno_callado_no_leyo_se_lee_despues(monkeypatch):
    # Con la IA apagada el turno calla ANTES de leer nada. Si el CRM vuelve a
    # mandar ese mensaje con la IA encendida, Nea tiene que leerlo: marcarlo al
    # recibir el despacho la dejaría contestando sin haberlo visto.
    ctx = cloud()
    contexto(monkeypatch, ai_enabled=False)
    await _procesar(ctx, despacho("dsp_1", ("msg_1", "hola")), durable=True)
    assert del_lead(ctx) == []
    assert ctx.llm.calls == []

    contexto(monkeypatch)
    await _procesar(
        ctx,
        despacho("dsp_2", ("msg_1", "hola"), ("msg_2", "¿hacen envíos?")),
        durable=True,
    )

    assert del_lead(ctx) == ["hola\n¿hacen envíos?"]
    await ctx.crm.aclose()


async def test_el_reintento_del_mismo_despacho_sigue_cubierto_por_su_id(monkeypatch):
    # Por la ruta sin cola (`durable=False`) el dedup del despacho va primero.
    ctx = cloud()
    contexto(monkeypatch)

    await _procesar(ctx, despacho("dsp_1", ("msg_1", "hola")))
    await _procesar(ctx, despacho("dsp_1", ("msg_1", "hola")))

    assert del_lead(ctx) == ["hola"]
    assert len(ctx.llm.calls) == 1
    await ctx.crm.aclose()


async def test_contra_la_raiz_tambien_se_anota(monkeypatch):
    # 031 — Desde la 2.0 la raíz también despacha, y también repite la ráfaga
    # completa: lo leído se anota igual que en cloud (sin organización).
    from app.state import InboundMessage
    from app.turn import run_turn

    ctx = make_ctx()
    monkeypatch.setattr(
        "app.turn._fetch_context", AsyncMock(return_value=crm_context(conv_id=CONV))
    )
    ctx.crm.post_typing = AsyncMock()
    ctx.crm.send_message = AsyncMock(return_value={"messageId": "msg_out"})

    await run_turn(
        ctx,
        IDENTIDAD,
        [InboundMessage(wa_message_id="wamid.1", identity=IDENTIDAD, type="text", text="hola")],
    )

    assert ctx.store.processed == {"msg::wamid.1"}
    await ctx.crm.aclose()


def test_la_marca_lleva_la_organizacion():
    # El id de un mensaje es de SU CRM: el mismo id en dos negocios no es el
    # mismo mensaje.
    assert marca_de_leido("org_a", "msg_1") != marca_de_leido("org_b", "msg_1")
    assert marca_de_leido("", "msg_1") == "msg::msg_1"

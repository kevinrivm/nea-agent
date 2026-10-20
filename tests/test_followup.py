"""Seguimiento: UN empujón y solo uno, marcado ANTES de enviar."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx

from app.followup import FollowupWorker
from app.llm import LlmReply
from app.state import utcnow
from app.turn import conversation_lock
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    crm_context,
    make_ctx,
    make_settings,
)


async def preparar_conversacion(ctx, hace_horas: float = 5.0):
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.add_message(conv.id, "user", "me interesa, luego te digo")
    await ctx.store.add_message(conv.id, "assistant", "va, ¿mañana o pasado?")
    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id=CRM_CONV_ID,
        greeted=True,
        followup_due_at=utcnow() - timedelta(hours=hace_horas - 4),
    )
    return conv


async def test_followup_un_solo_empujon(respx_mock):
    ctx = make_ctx()
    ctx.llm.replies = [LlmReply(content="¿Seguimos donde nos quedamos? 😊")]
    conv = await preparar_conversacion(ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    worker = FollowupWorker(ctx)

    await worker.tick()
    assert messages.call_count == 1
    assert ctx.store.conversations[conv.id].followup_sent is True

    # segundo barrido, y hasta un due_at fantasma: jamás un segundo empujón
    await ctx.store.update_conversation(
        conv.id, followup_due_at=utcnow() - timedelta(minutes=1)
    )
    await worker.tick()
    await worker.tick()
    assert messages.call_count == 1
    await ctx.crm.aclose()


async def test_followup_ventana_cerrada_se_omite_y_consume(respx_mock):
    ctx = make_ctx()
    conv = await preparar_conversacion(ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context(window_open=False))
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    worker = FollowupWorker(ctx)
    await worker.tick()
    assert messages.call_count == 0  # omitido con log
    # el claim ocurrió ANTES del envío: aunque "crashee" después, a lo sumo uno
    assert ctx.store.conversations[conv.id].followup_sent is True
    await worker.tick()
    assert messages.call_count == 0
    await ctx.crm.aclose()


async def test_followup_no_aplica_a_conversacion_cerrada(respx_mock):
    ctx = make_ctx()
    conv = await preparar_conversacion(ctx)
    await ctx.store.update_conversation(conv.id, phase="cerrada")
    context_route = respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    worker = FollowupWorker(ctx)
    await worker.tick()
    assert context_route.call_count == 0  # ni siquiera se consideró
    assert ctx.store.conversations[conv.id].followup_sent is False
    await ctx.crm.aclose()


async def test_followup_llm_caido_se_omite_sin_reintento_futuro(respx_mock):
    from app.llm import LlmExhausted

    ctx = make_ctx()
    ctx.llm.raise_exc = LlmExhausted("apagado")
    conv = await preparar_conversacion(ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    worker = FollowupWorker(ctx)
    await worker.tick()
    assert messages.call_count == 0
    assert ctx.store.conversations[conv.id].followup_sent is True  # consumido
    await ctx.crm.aclose()


async def test_followup_espera_el_turno_vivo_y_se_calla_si_el_lead_escribio(respx_mock):
    """El empujón se reclama antes de enviarlo. Si mientras espera su turno el
    lead escribe, el "¿seguimos?" queda fuera de lugar — y encimado con la
    respuesta del turno vivo."""
    ctx = make_ctx()
    ctx.llm.replies = [LlmReply(content="¿Seguimos donde nos quedamos? 😊")]
    conv = await preparar_conversacion(ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )

    # Un turno del lead ya está en vuelo: tiene el candado tomado.
    async with conversation_lock(ctx, IDENTITY):
        tarea = asyncio.create_task(FollowupWorker(ctx).tick())
        await asyncio.sleep(0.05)
        assert messages.call_count == 0, "el empujón no esperó al turno vivo"
        # Ese turno registra el mensaje del lead y luego suelta el candado.
        await ctx.store.update_conversation(conv.id, last_inbound_at=utcnow())
    await tarea

    assert messages.call_count == 0  # ya hubo conversación: sin empujón
    await ctx.crm.aclose()


# ── La lista de permitidos (032) ──────────────────────────────────────────


async def _seguimiento_con_lista(respx_mock, allowed_wa_ids: str):
    ctx = make_ctx(make_settings(allowed_wa_ids=allowed_wa_ids))
    ctx.llm.replies = [LlmReply(content="¿Seguimos donde nos quedamos? 😊")]
    conv = await preparar_conversacion(ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    worker = FollowupWorker(ctx)
    await worker.tick()
    return ctx, conv, worker, messages


async def test_followup_sale_si_la_identidad_esta_en_la_lista(respx_mock):
    ctx, _, _, messages = await _seguimiento_con_lista(respx_mock, "5215550001111")
    assert messages.call_count == 1
    await ctx.crm.aclose()


async def test_followup_fuera_de_la_lista_se_omite_y_consume(respx_mock):
    ctx, conv, worker, messages = await _seguimiento_con_lista(
        respx_mock, "525559999999"
    )
    assert messages.call_count == 0
    assert ctx.store.conversations[conv.id].followup_sent is True
    await worker.tick()  # consumido: el barrido siguiente tampoco lo manda
    assert messages.call_count == 0
    await ctx.crm.aclose()


async def test_followup_con_lista_vacia_sale_a_todos(respx_mock):
    ctx, _, _, messages = await _seguimiento_con_lista(respx_mock, "")
    assert messages.call_count == 1
    await ctx.crm.aclose()

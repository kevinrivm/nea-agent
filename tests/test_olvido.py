"""El CRM elimina una conversación y Nea la olvida.

Dos caminos, y los dos tienen que existir:

- **El aviso.** El CRM manda `conversation.deleted` por la ruta de los
  despachos. Nea lo guarda, acusa recibo y lo atiende en su cola.
- **La red.** Si el aviso no llega nunca, lo que delata el borrado es que la
  misma persona vuelve con una conversación de id distinto. El turno lo nota y
  empieza de cero.

`CUERPO_GEMELO` y `FIRMA_GEMELA` están copiados byte a byte en
`tests/unit/olvido-contrato.test.ts` de vocerocrm-cloud: es el contrato entre
los dos repositorios. Cambiar uno obliga a cambiar el otro.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx

from app.dispatch_worker import drain_one
from app.main import create_app
from app.state import OfferedSlot
from tests.conftest import (
    CRM_URL,
    IDENTITY,
    crm_context,
    make_ctx,
    make_settings,
    mock_crm_basics,
    wa_body,
)

SECRETO_GEMELO = "secreto-gemelo-del-olvido"
CUERPO_GEMELO = (
    b'{"type":"conversation.deleted","eventId":"olv_gemelo1",'
    b'"organization":{"id":"org_gemela","slug":"gemela"},'
    b'"conversation":{"id":"cv_gemela1","channel":"whatsapp"},'
    b'"contact":{"id":"ct_gemelo1","identity":"5215550001111"},'
    b'"deletedAt":"2026-10-04T12:00:00.000Z"}'
)
FIRMA_GEMELA = "sha256=72adddfb8dbc0ad6f091adff55132be228ec4be16be05fb4dde9b34010b65ffa"

IDENTIDAD = "5215550001111"
CONV = "cv_gemela1"


def cloud(**ajustes):
    return make_ctx(
        make_settings(
            vocero_mode="cloud",
            crm_organization="gemela",
            crm_brain_secret=SECRETO_GEMELO,
            **ajustes,
        )
    )


def firmar(cuerpo: bytes, secreto: str = SECRETO_GEMELO) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


async def enviar(client, cuerpo: bytes, firma: str | None = None):
    return await client.post(
        "/vocero/dispatch",
        content=cuerpo,
        headers={"x-vocero-signature": firma or firmar(cuerpo)},
    )


def cliente(ctx):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://test"
    )


async def con_memoria(ctx, org_id: str = "", crm_conversation_id: str | None = CONV):
    """Una conversación con todo lo que Nea guarda de ella."""
    conv = await ctx.store.get_or_create_conversation(IDENTIDAD, org_id, org_id)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id=crm_conversation_id, greeted=True, phase="agendando"
    )
    await ctx.store.add_message(conv.id, "user", "tengo una clínica dental")
    await ctx.store.add_message(conv.id, "assistant", "¿cuántos pacientes atiendes?")
    await ctx.store.replace_offered_slots(
        conv.id,
        [OfferedSlot(conv.id, datetime(2026, 10, 6, 16, tzinfo=timezone.utc), None, "martes 10:00")],
    )
    await ctx.store.enqueue_pending_send(conv.id, CONV, "respuesta que no salió")
    return conv


# ── El contrato ───────────────────────────────────────────────────────────


def test_la_firma_gemela_es_la_del_cuerpo_gemelo():
    # Si esto falla, alguien cambió el cuerpo o el secreto de un solo lado.
    assert firmar(CUERPO_GEMELO) == FIRMA_GEMELA


async def test_el_evento_gemelo_se_acepta_y_borra_la_memoria():
    ctx = cloud()
    conv = await con_memoria(ctx)
    async with cliente(ctx) as client:
        resp = await enviar(client, CUERPO_GEMELO, FIRMA_GEMELA)
        assert resp.status_code == 200
        # Acusar recibo NO es haber olvidado: eso lo hace la cola.
        assert conv.id in ctx.store.conversations
        assert await drain_one(ctx)
    assert ctx.store.conversations == {}
    assert ctx.store.messages == []
    assert ctx.store.offered == {}
    assert ctx.store.pending_sends == {}
    await ctx.crm.aclose()


# ── La ruta ───────────────────────────────────────────────────────────────


async def test_sin_firma_valida_no_se_olvida_nada():
    ctx = cloud()
    await con_memoria(ctx)
    async with cliente(ctx) as client:
        resp = await enviar(client, CUERPO_GEMELO, firmar(CUERPO_GEMELO, "otro-secreto"))
        assert resp.status_code == 401
        assert not await drain_one(ctx)
    assert len(ctx.store.conversations) == 1
    await ctx.crm.aclose()


async def test_un_evento_incompleto_es_422():
    ctx = cloud()
    async with cliente(ctx) as client:
        for falta in ("eventId", "conversation", "contact"):
            evento = json.loads(CUERPO_GEMELO)
            del evento[falta]
            resp = await enviar(client, json.dumps(evento).encode())
            assert resp.status_code == 422, falta
    assert ctx.store._inbox() == {}
    await ctx.crm.aclose()


async def test_el_reintento_del_crm_no_duplica():
    ctx = cloud()
    async with cliente(ctx) as client:
        assert (await enviar(client, CUERPO_GEMELO)).status_code == 200
        assert (await enviar(client, CUERPO_GEMELO)).status_code == 200
    assert len(ctx.store.dispatch_inbox) == 1
    await ctx.crm.aclose()


async def test_si_no_se_puede_guardar_no_se_acusa_recibo(monkeypatch):
    ctx = cloud()
    monkeypatch.setattr(
        ctx.store, "enqueue_dispatch", AsyncMock(side_effect=RuntimeError("base caída"))
    )
    async with cliente(ctx) as client:
        assert (await enviar(client, CUERPO_GEMELO)).status_code == 503
    await ctx.crm.aclose()


# ── La cola ───────────────────────────────────────────────────────────────


async def test_olvidar_no_es_un_turno_ni_un_handoff(monkeypatch):
    ctx = cloud()
    await con_memoria(ctx)
    turno = AsyncMock()
    monkeypatch.setattr("app.dispatch._procesar", turno)
    ctx.crm.post_handoff = AsyncMock()
    async with cliente(ctx) as client:
        await enviar(client, CUERPO_GEMELO)
    assert await drain_one(ctx)
    turno.assert_not_called()
    # La conversación ya no existe en el CRM: no hay a quién pasársela.
    ctx.crm.post_handoff.assert_not_called()
    await ctx.crm.aclose()


async def test_un_olvido_interrumpido_se_repite_al_recuperarlo():
    # Al revés que un turno, que al recuperarse solo pasa a humano: repetir un
    # borrado no manda nada dos veces.
    ctx = cloud()
    await con_memoria(ctx)
    async with cliente(ctx) as client:
        await enviar(client, CUERPO_GEMELO)
    async with ctx.store.claim_dispatch() as job:
        assert job and not job.recovery  # el proceso muere aquí, sin terminar
    fila = next(iter(ctx.store.dispatch_inbox.values()))
    fila["available_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    ctx.crm.post_handoff = AsyncMock()
    assert await drain_one(ctx)
    assert ctx.store.conversations == {}
    ctx.crm.post_handoff.assert_not_called()
    assert fila["state"] == "done"
    await ctx.crm.aclose()


async def test_se_van_tambien_los_despachos_guardados_de_esa_conversacion():
    # La cola guarda el cuerpo de cada despacho, con los mensajes del lead.
    ctx = cloud()
    viejo = {
        "dispatchId": "dsp_viejo",
        "organization": {"id": "org_gemela", "slug": "gemela"},
        "conversation": {"id": CONV},
        "contact": {"identity": IDENTIDAD},
        "messages": [{"id": "m1", "type": "text", "text": "mi número de cuenta es…"}],
    }
    ajeno = {**viejo, "dispatchId": "dsp_ajeno", "conversation": {"id": "cv_otra"}}
    for fila in (viejo, ajeno):
        await ctx.store.enqueue_dispatch("org_gemela", fila)
        ctx.store.dispatch_inbox[("org_gemela", fila["dispatchId"])]["state"] = "done"
    async with cliente(ctx) as client:
        await enviar(client, CUERPO_GEMELO)
    assert await drain_one(ctx)
    assert set(ctx.store.dispatch_inbox) == {
        ("org_gemela", "dsp_ajeno"),
        ("org_gemela", "olvido:olv_gemelo1"),
    }
    await ctx.crm.aclose()


async def test_un_aviso_tardio_no_borra_la_conversacion_nueva():
    # La persona volvió a escribir antes de que el aviso se atendiera: la fila
    # ya es de la conversación NUEVA, y esa nadie pidió borrarla.
    ctx = cloud()
    conv = await con_memoria(ctx, crm_conversation_id="cv_la_nueva")
    async with cliente(ctx) as client:
        await enviar(client, CUERPO_GEMELO)
    assert await drain_one(ctx)
    assert conv.id in ctx.store.conversations
    assert len(ctx.store.messages) == 2
    await ctx.crm.aclose()


async def test_multiorg_solo_olvida_en_la_organizacion_del_evento():
    # La misma persona le escribe a dos negocios: borrar en uno no toca al otro.
    ctx = make_ctx(
        make_settings(vocero_mode="cloud", crm_organization="", crm_brain_secret=SECRETO_GEMELO)
    )
    assert ctx.settings.multi_org
    borrada = await con_memoria(ctx, "org_gemela")
    intacta = await con_memoria(ctx, "org_vecina")
    async with cliente(ctx) as client:
        assert (await enviar(client, CUERPO_GEMELO)).status_code == 200
    assert await drain_one(ctx)
    assert borrada.id not in ctx.store.conversations
    assert intacta.id in ctx.store.conversations
    assert {m.conversation_id for m in ctx.store.messages} == {intacta.id}
    await ctx.crm.aclose()


async def test_multiorg_sin_organizacion_es_422():
    ctx = make_ctx(
        make_settings(vocero_mode="cloud", crm_organization="", crm_brain_secret=SECRETO_GEMELO)
    )
    evento = json.loads(CUERPO_GEMELO)
    del evento["organization"]
    async with cliente(ctx) as client:
        assert (await enviar(client, json.dumps(evento).encode())).status_code == 422
    await ctx.crm.aclose()


# ── La red: el aviso nunca llegó ──────────────────────────────────────────


def _historial(llamada: dict) -> str:
    return "\n".join(str(m.get("content")) for m in llamada["messages"][1:])


async def test_una_conversacion_nueva_del_crm_no_hereda_la_memoria(ctx, client, respx_mock):
    rutas = mock_crm_basics(respx_mock, conv_id="cv_primera")
    await client.post("/webhook", content=wa_body(text="vendo seguros de auto", wamid="wamid.a"))
    await asyncio.sleep(0.25)
    primera = next(iter(ctx.store.conversations.values()))
    assert primera.crm_conversation_id == "cv_primera" and primera.greeted

    # El dueño elimina la conversación en el CRM y Nea no se entera. La
    # persona vuelve a escribir: el CRM le abre una conversación nueva.
    rutas["context"].mock(
        return_value=httpx.Response(200, json=crm_context(conv_id="cv_segunda"))
    )
    await client.post("/webhook", content=wa_body(text="hola", wamid="wamid.b"))
    await asyncio.sleep(0.25)

    assert len(ctx.llm.calls) == 2
    assert "seguros de auto" in _historial(ctx.llm.calls[0])
    assert "seguros de auto" not in _historial(ctx.llm.calls[1])
    conv = next(iter(ctx.store.conversations.values()))
    assert conv.crm_conversation_id == "cv_segunda"
    assert {m.conversation_id for m in ctx.store.messages} == {conv.id}


async def test_la_misma_conversacion_conserva_su_memoria(ctx, client, respx_mock):
    # La red no puede dispararse en el caso de todos los días.
    mock_crm_basics(respx_mock)
    await client.post("/webhook", content=wa_body(text="vendo seguros de auto", wamid="wamid.a"))
    await asyncio.sleep(0.25)
    await client.post("/webhook", content=wa_body(text="¿y cuánto cuesta?", wamid="wamid.b"))
    await asyncio.sleep(0.25)
    assert len(ctx.llm.calls) == 2
    assert "seguros de auto" in _historial(ctx.llm.calls[1])
    assert len(ctx.store.conversations) == 1


async def test_en_cloud_un_cierre_viejo_no_calla_a_la_conversacion_nueva(monkeypatch):
    # El cierre por falta de rumbo se revisa ANTES de pedirle el contexto al
    # CRM. Si la memoria vieja no se olvidara antes de ese gate, el primer
    # «hola» de la conversación nueva se quedaría sin respuesta.
    from app.turn import run_turn, _Turno
    from app.state import InboundMessage, utcnow

    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_eliminada", stalled_at=utcnow()
    )
    await ctx.store.add_message(conv.id, "user", "historial de la eliminada")
    contexto = AsyncMock(return_value=crm_context(conv_id="cv_nueva"))
    monkeypatch.setattr("app.turn._fetch_context", contexto)
    ctx.crm.post_typing = AsyncMock()
    ctx.crm.send_message = AsyncMock(return_value={"messageId": "msg_1"})

    await run_turn(
        ctx,
        IDENTITY,
        [InboundMessage(wa_message_id="m1", identity=IDENTITY, type="text", text="ok")],
        _Turno(crm_conversation_id="cv_nueva"),
    )

    contexto.assert_awaited()  # pasó el gate del cierre: no hubo silencio
    assert len(ctx.llm.calls) == 1
    assert "historial de la eliminada" not in _historial(ctx.llm.calls[0])
    await ctx.crm.aclose()

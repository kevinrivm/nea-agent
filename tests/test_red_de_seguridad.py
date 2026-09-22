"""La red de seguridad del turno: si revienta, un humano se entera.

`handle_flush` ya tragaba cualquier excepción del turno (jamás tumba el loop),
pero solo dejaba el log: el lead se quedaba sin respuesta y sin nadie que lo
viera. Ahora, además, registra un handoff `error` en el CRM cuando sabe de qué
conversación era — por el cliente del turno, igual que el handoff normal:
`/api/bot` en el modo de siempre y `/api/brains` con la credencial de SU
organización en cloud.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from app import dispatch, turn
from app.config import Settings
from app.crm_brains import BrainsCrmClient
from app.llm import LlmReply, ToolCall
from app.multiorg import (
    CrmSinOrganizacion,
    LlmSinOrganizacion,
    RegistroDeOrganizaciones,
    credencial_derivada,
)
from app.state import AppContext, InboundMessage, MemoryStore
from app.turn import handle_flush
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    FakeLLM,
    make_ctx,
    make_settings,
    mock_crm_basics,
)

SECRETO = "secreto-del-despliegue-de-32-caracteres"
CONV = "cv_nube1"


def _mensajes(*wamids: str) -> list[InboundMessage]:
    return [
        InboundMessage(wa_message_id=w, identity=IDENTITY, type="text", text="hola")
        for w in wamids
    ]


class _StoreQueFalla(MemoryStore):
    """Una base que se cae justo al abrir el turno."""

    async def get_or_create_conversation(self, *_a, **_k):  # type: ignore[override]
        raise ConnectionError("la base no responde")


# ── El modo de siempre ────────────────────────────────────────────────────


async def test_un_fallo_inesperado_pasa_la_conversacion_a_un_humano(respx_mock, caplog):
    rutas = mock_crm_basics(respx_mock)
    ctx = make_ctx()
    ctx.llm.raise_exc = RuntimeError("un fallo de código")  # no es LlmExhausted
    caplog.set_level(logging.INFO, logger="nea.turn")

    try:
        await handle_flush(ctx, IDENTITY, _mensajes("wamid.x1", "wamid.x2"))
    finally:
        await ctx.crm.aclose()

    assert rutas["messages"].call_count == 0  # nada roto al lead
    assert rutas["handoff"].call_count == 1
    enviado = json.loads(rutas["handoff"].calls[0].request.content)
    assert enviado == {"conversationId": CRM_CONV_ID, "reason": "error"}
    # Y el log dice qué ráfaga fue, para encontrarla.
    assert "wamid.x1, wamid.x2" in caplog.text
    assert "handoff error registrado" in caplog.text


async def test_sin_conversacion_conocida_no_hay_a_quien_avisarle(respx_mock, caplog):
    """Antes de leer el contexto el turno no sabe qué conversación del CRM es:
    se queda el log, y no se inventa un destino."""
    rutas = mock_crm_basics(respx_mock)
    ctx = make_ctx()
    ctx.store = _StoreQueFalla()
    caplog.set_level(logging.INFO, logger="nea.turn")

    try:
        await handle_flush(ctx, IDENTITY, _mensajes("wamid.y1"))
    finally:
        await ctx.crm.aclose()

    assert rutas["handoff"].call_count == 0
    assert "wamid.y1" in caplog.text
    assert "no sé de qué conversación" in caplog.text


@pytest.mark.parametrize("como_falla", ["500", "excepcion"])
async def test_si_el_handoff_tambien_falla_no_revienta(respx_mock, como_falla):
    rutas = mock_crm_basics(respx_mock)
    ctx = make_ctx()
    ctx.llm.raise_exc = RuntimeError("un fallo de código")
    if como_falla == "500":
        rutas["handoff"].mock(return_value=httpx.Response(500))
    else:
        rutas["handoff"].mock(side_effect=RuntimeError("algo raro en el cliente"))

    try:
        await handle_flush(ctx, IDENTITY, _mensajes("wamid.z1"))  # no lanza
    finally:
        await ctx.crm.aclose()

    assert rutas["handoff"].call_count == 1


async def test_no_pisa_el_handoff_que_el_turno_ya_hizo(respx_mock, monkeypatch):
    """El motivo es lo que el dueño lee: «el cliente pidió humano» no puede
    convertirse en «error» porque algo falló después."""
    rutas = mock_crm_basics(respx_mock)
    ctx = make_ctx()
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="h", name="handoff", arguments={"reason": "pidió humano"})]),
        LlmReply(content="Va, te paso con el equipo."),
    ]
    original = ctx.store.update_conversation

    async def falla_al_cerrar(conversation_id, **campos):
        if "greeted" in campos:  # la escritura del final del turno
            raise ConnectionError("la base se cayó al final")
        await original(conversation_id, **campos)

    monkeypatch.setattr(ctx.store, "update_conversation", falla_al_cerrar)

    try:
        await handle_flush(ctx, IDENTITY, _mensajes("wamid.h1"))
    finally:
        await ctx.crm.aclose()

    assert rutas["messages"].call_count == 1
    motivos = [json.loads(c.request.content)["reason"] for c in rutas["handoff"].calls]
    assert motivos == ["cliente"]


async def test_un_turno_que_sale_bien_no_registra_handoff(respx_mock):
    rutas = mock_crm_basics(respx_mock)
    ctx = make_ctx()
    try:
        await handle_flush(ctx, IDENTITY, _mensajes("wamid.ok"))
    finally:
        await ctx.crm.aclose()
    assert rutas["messages"].call_count == 1
    assert rutas["handoff"].call_count == 0


# ── Cloud ─────────────────────────────────────────────────────────────────


def _contexto_cloud(con_llm: bool = False) -> dict:
    datos = {
        "contact": {"id": "ct_1", "name": "Ana", "ficha": {}},
        "conversation": {"id": CONV, "aiEnabled": True, "windowOpen": True},
        "agent": {"name": "Nea"},
        "knowledge": [],
    }
    if con_llm:
        datos["llm"] = {"path": "/api/brains/llm", "model": "a/b"}
    return datos


def _despacho(organizacion: dict | None = None) -> dict:
    payload = {
        "dispatchId": "dsp_1",
        "conversation": {"id": CONV},
        "contact": {"identity": IDENTITY, "displayName": "Ana"},
        "messages": [{"id": "msg_1", "type": "text", "text": "hola"}],
    }
    if organizacion:
        payload["organization"] = organizacion
    return payload


def _rutas_cloud(respx_mock, con_llm: bool = False) -> dict:
    return {
        "context": respx_mock.get(f"{CRM_URL}/api/brains/context").mock(
            return_value=httpx.Response(200, json=_contexto_cloud(con_llm))
        ),
        "typing": respx_mock.post(f"{CRM_URL}/api/brains/typing").mock(
            return_value=httpx.Response(200, json={"ok": True})
        ),
        "messages": respx_mock.post(f"{CRM_URL}/api/brains/messages").mock(
            return_value=httpx.Response(201, json={"ok": True})
        ),
        "handoff": respx_mock.post(f"{CRM_URL}/api/brains/handoff").mock(
            return_value=httpx.Response(200, json={"ok": True})
        ),
        "bot": respx_mock.route(url__startswith=f"{CRM_URL}/api/bot/").mock(
            return_value=httpx.Response(500)
        ),
    }


def _ctx_cloud_de_un_negocio(store=None) -> AppContext:
    return AppContext(
        settings=make_settings(
            vocero_mode="cloud", crm_organization="negocio-a", crm_brain_secret=SECRETO
        ),
        store=store or MemoryStore(),
        crm=BrainsCrmClient(CRM_URL, SECRETO, "negocio-a"),
        llm=FakeLLM(),
    )


async def test_cloud_el_handoff_de_emergencia_va_por_el_cerebro(respx_mock):
    rutas = _rutas_cloud(respx_mock)
    ctx = _ctx_cloud_de_un_negocio()
    ctx.llm.raise_exc = RuntimeError("un fallo de código")

    try:
        await dispatch._procesar(ctx, _despacho())
    finally:
        await ctx.crm.aclose()

    assert rutas["messages"].call_count == 0
    assert rutas["bot"].call_count == 0  # jamás por la superficie de un solo negocio
    assert rutas["handoff"].call_count == 1
    peticion = rutas["handoff"].calls[0].request
    assert json.loads(peticion.content) == {"conversationId": CONV, "reason": "error"}
    assert peticion.headers["authorization"] == f"Bearer {SECRETO}"
    assert peticion.headers["x-vocero-organization"] == "negocio-a"


async def test_cloud_sabe_la_conversacion_desde_el_despacho(respx_mock):
    """En cloud el CRM ya dijo qué conversación es: aunque el turno reviente
    antes de leer el contexto, hay a quién pasársela."""
    rutas = _rutas_cloud(respx_mock)
    ctx = _ctx_cloud_de_un_negocio(store=_StoreQueFalla())

    try:
        await dispatch._procesar(ctx, _despacho())
    finally:
        await ctx.crm.aclose()

    assert rutas["context"].call_count == 0  # reventó antes
    assert rutas["handoff"].call_count == 1
    assert json.loads(rutas["handoff"].calls[0].request.content)["reason"] == "error"


async def test_multiorg_el_handoff_lleva_la_credencial_de_esa_organizacion(
    respx_mock, monkeypatch
):
    """El cliente del turno, no el del arranque: con el centinela esto
    reventaría, y con el de otra organización le escribiría al negocio que no
    era."""
    rutas = _rutas_cloud(respx_mock, con_llm=True)

    def prompt_roto(**_kwargs):
        raise RuntimeError("un fallo de código armando el prompt")

    monkeypatch.setattr(turn, "build_system_prompt", prompt_roto)
    ctx = AppContext(
        settings=Settings(
            _env_file=None, vocero_mode="cloud", crm_organization="",
            crm_brain_secret=SECRETO, crm_base_url=CRM_URL,
        ),
        store=MemoryStore(),
        crm=CrmSinOrganizacion(),
        llm=LlmSinOrganizacion(),
        registro=RegistroDeOrganizaciones(CRM_URL, SECRETO),
    )

    try:
        await dispatch._procesar(ctx, _despacho({"id": "org_1", "slug": "negocio-a"}))
    finally:
        await ctx.registro.aclose()

    assert rutas["handoff"].call_count == 1
    peticion = rutas["handoff"].calls[0].request
    assert json.loads(peticion.content) == {"conversationId": CONV, "reason": "error"}
    assert peticion.headers["authorization"] == (
        f"Bearer {credencial_derivada(SECRETO, 'org_1')}"
    )
    assert peticion.headers["x-vocero-organization"] == "negocio-a"

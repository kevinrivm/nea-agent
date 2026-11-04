"""038 — Nea mueve al lead de etapa (`POST /api/bot/stage` de vocero-crm).

La herramienta `move_stage` se le enseña al modelo solo si el perfil tiene
`etapa` encendida y el contexto del turno trae `lead.nextStages` con algo: un
CRM sin la ruta (cloud, o un raíz anterior) no manda esa llave y la
herramienta no existe. Las reglas (solo hacia adelante, herramienta encendida)
las aplica el CRM; su 409/422 vuelve al modelo para que se corrija.
"""
from __future__ import annotations

import json

import httpx

from app.llm import LlmReply, ToolCall
from app.main import create_app
from app.profile import BusinessProfile
from app.tools import CLAVE_DE_HERRAMIENTA, etapas_siguientes
from tests.conftest import CRM_CONV_ID, CRM_URL, crm_context, despachar, make_ctx, mock_crm_basics

SIGUIENTES = [{"id": "st_calif", "name": "Calificado"}, {"id": "st_cita", "name": "Cita agendada"}]


class _Perfil:
    def __init__(self, perfil: BusinessProfile) -> None:
        self._p = perfil

    async def get(self) -> BusinessProfile:
        return self._p


def _nombres(schemas):
    return {t["function"]["name"] for t in schemas or []}


def _con_etapas(respx_mock, siguientes=SIGUIENTES):
    """Las rutas de siempre, con `lead.nextStages` en el contexto."""
    rutas = mock_crm_basics(respx_mock)
    contexto = crm_context()
    contexto["lead"]["nextStages"] = siguientes
    rutas["context"].mock(return_value=httpx.Response(200, json=contexto))
    return rutas


async def _turno(ctx, texto="ya sé lo que quiero, es para este mes"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    ) as client:
        await despachar(client, ctx, esperar=True, text=texto)


def test_etapas_siguientes_del_contexto():
    assert etapas_siguientes({"lead": {"nextStages": SIGUIENTES}}) == ["Calificado", "Cita agendada"]
    assert etapas_siguientes({"lead": {"stageName": "Nuevo"}}) == []
    assert etapas_siguientes({"lead": {"nextStages": [{"name": " "}, "x", {"id": 1}]}}) == []
    assert etapas_siguientes(None) == []


def test_move_stage_es_de_la_clave_etapa():
    assert CLAVE_DE_HERRAMIENTA["move_stage"] == "etapa"


async def test_con_etapas_y_encendida_se_ensena_y_mueve(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi"))
    _con_etapas(respx_mock)
    stage = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "changed": True, "lead": {"id": "ld_1", "stageName": "Calificado"}}
        )
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="m", name="move_stage", arguments={"etapa": "Calificado"})]),
        LlmReply(content="Perfecto, te ayudo con eso."),
    ]
    await _turno(ctx)

    primera = ctx.llm.calls[0]
    assert "move_stage" in _nombres(primera["tools"])
    assert "move_stage a: Calificado, Cita agendada" in primera["messages"][0]["content"]
    assert stage.called
    assert json.loads(stage.calls[0].request.content) == {"conversationId": CRM_CONV_ID, "stage": "Calificado"}
    resultado = json.loads(ctx.llm.calls[1]["messages"][-1]["content"])
    assert resultado["ok"] is True and resultado["etapa"] == "Calificado"
    await ctx.crm.aclose()


async def test_el_rechazo_del_crm_vuelve_al_modelo(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi"))
    rutas = _con_etapas(respx_mock)
    respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        return_value=httpx.Response(
            409,
            json={"error": {"code": "etapa_no_permitida", "message": "Puedes moverlo a: Calificado."}},
        )
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="m", name="move_stage", arguments={"etapa": "Cliente"})]),
        LlmReply(content="Claro, sigamos."),
    ]
    await _turno(ctx)

    resultado = json.loads(ctx.llm.calls[1]["messages"][-1]["content"])
    assert resultado == {"ok": False, "error": "etapa_no_permitida", "detalle": "Puedes moverlo a: Calificado."}
    # El turno sigue: el lead recibe su respuesta.
    assert rutas["messages"].called
    await ctx.crm.aclose()


async def test_apagada_en_el_perfil_no_se_ensena(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi", tools=frozenset({"ficha", "salida"})))
    _con_etapas(respx_mock)
    await _turno(ctx)
    llamada = ctx.llm.calls[0]
    assert "move_stage" not in _nombres(llamada["tools"])
    assert "move_stage" not in llamada["messages"][0]["content"]
    await ctx.crm.aclose()


async def test_sin_next_stages_no_existe(respx_mock):
    """Un CRM sin la ruta (cloud, un raíz anterior) no manda `nextStages`."""
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi"))
    mock_crm_basics(respx_mock)
    await _turno(ctx)
    assert "move_stage" not in _nombres(ctx.llm.calls[0]["tools"])
    await ctx.crm.aclose()


async def test_en_la_ultima_etapa_tampoco(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi"))
    _con_etapas(respx_mock, siguientes=[])
    await _turno(ctx)
    assert "move_stage" not in _nombres(ctx.llm.calls[0]["tools"])
    await ctx.crm.aclose()


def test_dice_cuando_mover_no_solo_que_se_puede():
    """Con «hazlo cuando la conversación lo amerite» y nada más, GLM y
    gpt-4o-mini nunca llamaron move_stage en la prueba en vivo (2026-10-21),
    ni con «me interesa contratarlos». El prompt y la herramienta tienen que
    dar criterios concretos, pedir que vaya en el mismo turno y decir cuándo NO.
    """
    from app.prompt import build_system_prompt
    from app.state import Conversation
    from app.tools import MOVE_STAGE_SCHEMA

    contexto = crm_context()
    contexto["lead"]["nextStages"] = SIGUIENTES
    prompt = build_system_prompt(
        profile=BusinessProfile(agent_name="Sofi"),
        context=contexto,
        conv=Conversation(id=1, wa_identity="5215550000000"),
    )
    linea = next(l for l in prompt.splitlines() if "move_stage a:" in l)
    for criterio in ("MISMO TURNO", "precio", "contratar", "agendar", "saludo"):
        assert criterio in linea, criterio
    descripcion = MOVE_STAGE_SCHEMA["function"]["description"]
    for criterio in ("MISMO", "precio", "contratar", "saludo"):
        assert criterio in descripcion, criterio

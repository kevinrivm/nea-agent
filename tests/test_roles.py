"""034 — Roles como datos: el CRM dice qué herramientas van encendidas.

`GET /api/bot/profile` trae `role` y `tools` (claves del catálogo del CRM).
Nea le enseña al modelo solo las encendidas; sin «agendar», además, se porta
como contra un CRM sin agenda. Sin `tools` (un CRM viejo), todas, como antes.
"""
from __future__ import annotations

from app.profile import BusinessProfile, profile_from_payload
from app.tools import CLAVE_DE_HERRAMIENTA, solo_encendidas, tool_schemas
from tests.conftest import despachar, make_ctx, mock_crm_basics

import httpx

from app.main import create_app


def _nombres(schemas):
    return {t["function"]["name"] for t in schemas}


def test_el_perfil_lee_role_y_tools():
    p = profile_from_payload(
        {"profile": {"name": "Sofi", "role": "calificador", "tools": ["ficha", "escalar", 3]}},
        "Nea",
    )
    assert p.role == "calificador"
    assert p.tools == frozenset({"ficha", "escalar"})
    assert p.permite("ficha") and not p.permite("agendar")


def test_un_crm_que_no_manda_tools_enciende_todo():
    p = profile_from_payload({"profile": {"name": "Sofi"}}, "Nea")
    assert p.tools is None
    assert all(p.permite(c) for c in ("ficha", "agendar", "reprogramar", "salida", "escalar"))


def test_escalar_no_se_apaga():
    assert BusinessProfile(tools=frozenset()).permite("escalar")


def test_solo_se_ensenan_las_encendidas():
    todas = tool_schemas(True)
    sin_agenda = solo_encendidas(todas, BusinessProfile(tools=frozenset({"ficha", "salida"})))
    assert _nombres(sin_agenda) == {"update_ficha", "route_out", "handoff"}
    assert _nombres(solo_encendidas(todas, BusinessProfile())) == _nombres(todas)


def test_cada_herramienta_tiene_su_clave():
    """Una herramienta nueva sin clave no se podría apagar desde el CRM."""
    for nombre in _nombres(tool_schemas(True, agenda_v2=True, coordination=True)):
        assert nombre in CLAVE_DE_HERRAMIENTA, nombre


class _Perfil:
    def __init__(self, perfil: BusinessProfile) -> None:
        self._p = perfil

    async def get(self) -> BusinessProfile:
        return self._p


async def test_sin_agendar_el_turno_no_ofrece_citas(respx_mock):
    """AC4: ni las herramientas de agenda ni la promesa en el prompt."""
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi", tools=frozenset({"ficha", "salida"})))
    mock_crm_basics(respx_mock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    ) as client:
        await despachar(client, ctx, esperar=True, text="quiero agendar una cita")
    llamada = ctx.llm.calls[0]
    assert _nombres(llamada["tools"]) == {"update_ficha", "route_out", "handoff"}
    assert "NO AGENDA POR AQUÍ" in llamada["messages"][0]["content"]
    await ctx.crm.aclose()


async def test_con_todo_encendido_el_turno_ofrece_citas(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi"))
    mock_crm_basics(respx_mock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    ) as client:
        await despachar(client, ctx, esperar=True, text="quiero agendar una cita")
    assert "propose_slots" in _nombres(ctx.llm.calls[0]["tools"])
    await ctx.crm.aclose()

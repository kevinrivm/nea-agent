"""Hostilidad sostenida (AC-18): léxico + backstop determinista del turno."""
from __future__ import annotations

import asyncio

import httpx

from app.hostility import hostile_streak, is_hostile
from app.main import create_app
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    make_ctx,
    mock_crm_basics,
    despachar,
)


def test_coloquial_mexicano_no_cuenta():
    for texto in (
        "no mames, qué chido quedó esto",
        "qué pedo, ¿cómo funciona?",
        "está bien vergas tu sistema",  # entusiasmo, no dirigido
        "me urge, ando hasta la madre de trabajo",
    ):
        assert not is_hostile(texto), texto


def test_agresion_dirigida_cuenta():
    for texto in (
        "vete mucho a la verga con tu asesoría",
        "pinche estafador",
        "esto es una estafa o qué pedo",
        "puro humo, pinches bots chafas",
        "chinga tu madre",
    ):
        assert is_hostile(texto), texto


def test_racha_se_corta_si_el_lead_se_calma():
    assert hostile_streak(["eres una estafa", "ok perdón, cuéntame más", "pinche bot"]) == 1


def test_racha_de_tres_al_final():
    assert (
        hostile_streak(["hola", "esto es estafa", "pinches bots chafas", "vete a la verga"])
        == 3
    )


async def test_tercer_strike_fuerza_handoff_aunque_el_llm_no_lo_llame(respx_mock):
    """El FakeLLM JAMÁS llama herramientas — el handoff sale del backstop."""
    ctx = make_ctx()
    routes = mock_crm_basics(respx_mock)

    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    hostiles = [
        "oye esto es una estafa o qué pedo",
        "no mames, puro humo, pinches bots chafas",
        "vete mucho a la verga con tu asesoría, pinche estafador",
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        for i, texto in enumerate(hostiles):
            await despachar(c, ctx, text=texto, wamid=f"wamid.host{i}")
            await asyncio.sleep(0.3)
    await ctx.crm.aclose()

    assert routes["handoff"].call_count == 1
    import json

    body = json.loads(routes["handoff"].calls[0].request.content)
    assert body.get("reason") == "hostilidad"
    # La despedida del tercer turno se envió ANTES de la pausa (3 respuestas).
    assert routes["messages"].call_count == 3
    # El turno del strike recibió la alerta del sistema.
    ultimo = ctx.llm.calls[-1]["messages"]
    assert any(
        m.get("role") == "system" and "TERCER" in str(m.get("content"))
        for m in ultimo
    )


async def test_dos_strikes_no_disparan_nada(respx_mock):
    ctx = make_ctx()
    routes = mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        for i, texto in enumerate(["esto es una estafa", "pinches bots chafas"]):
            await despachar(c, ctx, text=texto, wamid=f"wamid.h2{i}")
            await asyncio.sleep(0.3)
    await ctx.crm.aclose()
    assert routes["handoff"].call_count == 0
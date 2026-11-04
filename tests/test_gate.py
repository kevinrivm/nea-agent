"""Gate del turno: allowlist (Constitución V), aiEnabled=false y ventana cerrada."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import create_app
from tests.conftest import (
    CRM_URL,
    crm_context,
    make_ctx,
    make_settings,
    mock_crm_basics,
    despachar,
)


@pytest.fixture
def allow_ctx():
    # allowlist con el formato 521... — debe matchear al 52... por canonicalización
    return make_ctx(make_settings(allowed_wa_ids="5215550001111"))


@pytest.fixture
async def allow_client(allow_ctx):
    app = create_app(ctx=allow_ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        yield c
    await allow_ctx.crm.aclose()


async def test_allowlist_fuera_sin_respuesta(allow_ctx, allow_client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    # identidad que NO está en la allowlist
    await despachar(allow_client, allow_ctx, frm="529999999999", wamid="wamid.out")
    await asyncio.sleep(0.2)
    assert routes["context"].call_count == 0  # ni siquiera consulta contexto
    assert routes["messages"].call_count == 0  # respuesta no
    assert len(allow_ctx.llm.calls) == 0


async def test_allowlist_dentro_con_canonicalizacion_521(
    allow_ctx, allow_client, respx_mock
):
    routes = mock_crm_basics(respx_mock)
    # Meta reporta 5255... (12 dígitos); la allowlist trae 52155... (13) — misma persona
    await despachar(allow_client, allow_ctx, frm="525550001111", wamid="wamid.in")
    await asyncio.sleep(0.2)
    assert routes["messages"].call_count == 1


async def test_ai_pausada_silencio(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock, ai_enabled=False)
    await despachar(client, ctx)
    await asyncio.sleep(0.2)
    assert routes["context"].call_count == 1  # sí consultó el contexto
    assert len(ctx.llm.calls) == 0  # pero no conversó
    assert routes["messages"].call_count == 0


async def test_ventana_cerrada_silencio(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock, window_open=False)
    await despachar(client, ctx)
    await asyncio.sleep(0.2)
    assert len(ctx.llm.calls) == 0
    assert routes["messages"].call_count == 0

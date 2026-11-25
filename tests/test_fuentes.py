"""vocerocrm-cloud 243 — Nea consulta las fuentes del negocio (`consultar_fuente`).

La herramienta solo existe si el contexto trae `sources` (las fuentes
encendidas). Busca por `POST /api/brains/fuentes/buscar`, contesta con lo que
devuelve, y la regla del dueño para «no lo encontró» la cumple el turno,
llame el modelo `handoff` o no.
"""
from __future__ import annotations

import httpx
import respx

from app.crm import CrmUnreachable
from app.crm_brains import BrainsCrmClient
from app.llm import LlmReply, ToolCall
from app.main import create_app
from app.prompt import _lineas_fuentes
from app.tools import (
    MAX_CONSULTAS_FUENTE,
    con_consultar_fuente,
    fuentes_del_contexto,
)
from tests.conftest import crm_context, despachar, make_ctx, mock_crm_basics

CATALOGO = {
    "id": "fu_catalogo",
    "name": "Catálogo de juegos",
    "whenToUse": "Cuando pregunten si un juego viene en la consola",
    "ifNotFound": "say",
}
MS6 = {"nombre": "Metal Slug 6", "consola": "Neo Geo", "anio": 2006}


def _nombres(schemas):
    return {t["function"]["name"] for t in schemas or []}


def _con_fuentes(respx_mock, fuentes):
    rutas = mock_crm_basics(respx_mock)
    contexto = crm_context()
    contexto["sources"] = fuentes
    rutas["context"].mock(return_value=httpx.Response(200, json=contexto))
    return rutas


def _crm_que_busca(ctx, respuestas):
    """El CRM del turno (raíz en las pruebas) con la búsqueda de cloud encima."""
    pedidas = []

    async def buscar(conversation_id, source_id, query):
        pedidas.append((source_id, query))
        r = respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return dict(r)

    ctx.crm.buscar_en_fuente = buscar
    return pedidas


async def _turno(ctx, texto):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    ) as client:
        await despachar(client, ctx, esperar=True, text=texto)


def _respuesta(found=(), similar=(), if_not_found="say"):
    return {
        "ok": True,
        "source": "Catálogo de juegos",
        "found": list(found),
        "similar": list(similar),
        "total": len(found),
        "ifNotFound": if_not_found,
    }


# ------------------------------------------------------------ piezas puras


def test_fuentes_del_contexto_descarta_lo_raro():
    assert fuentes_del_contexto({"sources": [CATALOGO]}) == [CATALOGO]
    assert fuentes_del_contexto({"sources": [{"id": "fu_x", "name": "X", "ifNotFound": "otra"}]}) == [
        {"id": "fu_x", "name": "X", "whenToUse": "", "ifNotFound": "say"}
    ]
    assert fuentes_del_contexto({"sources": [{"id": "", "name": "X"}, "x", {"name": "Y"}]}) == []
    assert fuentes_del_contexto({}) == []
    assert fuentes_del_contexto(None) == []


def test_sin_fuentes_no_hay_herramienta_ni_regla():
    assert con_consultar_fuente([], []) == []
    assert _lineas_fuentes({"sources": []}) == []
    assert _lineas_fuentes(None) == []


def test_con_fuentes_la_herramienta_lista_ids_y_cuando_usarlas():
    [schema] = con_consultar_fuente([], [CATALOGO])
    f = schema["function"]
    assert f["name"] == "consultar_fuente"
    assert f["parameters"]["properties"]["fuente"]["enum"] == ["fu_catalogo"]
    assert "fu_catalogo: Catálogo de juegos — Cuando pregunten si un juego viene" in f["description"]
    regla = " ".join(_lineas_fuentes({"sources": [CATALOGO]}))
    assert "«Catálogo de juegos»" in regla
    assert "ANTES de contestar" in regla


# ------------------------------------------------------------ el turno


async def test_consulta_y_contesta_con_lo_que_encontro(respx_mock):
    ctx = make_ctx()
    rutas = _con_fuentes(respx_mock, [CATALOGO])
    pedidas = _crm_que_busca(ctx, [_respuesta(found=[MS6])])
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="f", name="consultar_fuente", arguments={"fuente": "fu_catalogo", "consulta": "metal slug 6"})],
        ),
        LlmReply(content="¡Sí viene! Metal Slug 6, en Neo Geo 🎮"),
    ]
    await _turno(ctx, "trae metal slug 6?")

    primera = ctx.llm.calls[0]
    assert "consultar_fuente" in _nombres(primera["tools"])
    assert "FUENTES DE CONSULTA" in primera["messages"][0]["content"]
    assert pedidas == [("fu_catalogo", "metal slug 6")]
    resultado = ctx.llm.calls[1]["messages"][-1]
    assert resultado["role"] == "tool"
    assert "Metal Slug 6" in resultado["content"]
    assert "contesta con estos datos" in resultado["content"]
    assert rutas["messages"].called
    assert not rutas["handoff"].called


async def test_sin_sources_el_turno_es_el_de_siempre(respx_mock):
    ctx = make_ctx()
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¡Hola! ¿En qué te ayudo?")]
    await _turno(ctx, "hola")
    primera = ctx.llm.calls[0]
    assert "consultar_fuente" not in _nombres(primera["tools"])
    assert "FUENTES DE CONSULTA" not in primera["messages"][0]["content"]


async def test_no_encontrado_con_regla_decir_no_traspasa(respx_mock):
    ctx = make_ctx()
    rutas = _con_fuentes(respx_mock, [CATALOGO])
    _crm_que_busca(ctx, [_respuesta(similar=[MS6])])
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="f", name="consultar_fuente", arguments={"fuente": "fu_catalogo", "consulta": "metal slug 7"})],
        ),
        LlmReply(content="Metal Slug 7 no viene, pero sí traemos el 6 en Neo Geo."),
    ]
    await _turno(ctx, "trae metal slug 7?")
    resultado = ctx.llm.calls[1]["messages"][-1]["content"]
    assert "no lo tienen" in resultado
    assert "alternativa" in resultado
    assert rutas["messages"].called
    assert not rutas["handoff"].called


async def test_no_encontrado_con_regla_equipo_traspasa_aunque_el_modelo_no(respx_mock):
    ctx = make_ctx()
    rutas = _con_fuentes(respx_mock, [{**CATALOGO, "ifNotFound": "handoff"}])
    _crm_que_busca(ctx, [_respuesta(if_not_found="handoff")])
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="f", name="consultar_fuente", arguments={"fuente": "fu_catalogo", "consulta": "metal slug 7"})],
        ),
        LlmReply(content="Déjame revisarlo con el equipo."),
    ]
    await _turno(ctx, "trae metal slug 7?")
    assert "llama handoff" in ctx.llm.calls[1]["messages"][-1]["content"]
    assert "NO digas que no lo tienen" in ctx.llm.calls[1]["messages"][-1]["content"]
    assert rutas["messages"].called
    assert rutas["handoff"].called


async def test_una_segunda_busqueda_que_encuentra_quita_el_traspaso(respx_mock):
    ctx = make_ctx()
    rutas = _con_fuentes(respx_mock, [{**CATALOGO, "ifNotFound": "handoff"}])
    _crm_que_busca(ctx, [_respuesta(), _respuesta(found=[{"nombre": "Pokemon - Yellow Version"}])])
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="a", name="consultar_fuente", arguments={"fuente": "fu_catalogo", "consulta": "pokemon amarillo"})],
        ),
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="b", name="consultar_fuente", arguments={"fuente": "fu_catalogo", "consulta": "pokemon yellow"})],
        ),
        LlmReply(content="¡Sí viene Pokémon Yellow!"),
    ]
    await _turno(ctx, "tienen pokemon amarillo?")
    assert rutas["messages"].called
    assert not rutas["handoff"].called


async def test_fuente_caida_traspasa_y_no_afirma(respx_mock):
    ctx = make_ctx()
    rutas = _con_fuentes(respx_mock, [CATALOGO])
    _crm_que_busca(ctx, [CrmUnreachable("timeout")])
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="f", name="consultar_fuente", arguments={"fuente": "fu_catalogo", "consulta": "metal slug 6"})],
        ),
        LlmReply(content="Lo confirmo con el equipo y te escriben por aquí."),
    ]
    await _turno(ctx, "trae metal slug 6?")
    resultado = ctx.llm.calls[1]["messages"][-1]["content"]
    assert "fuente_no_disponible" in resultado
    assert "NO digas" in resultado
    assert rutas["handoff"].called


async def test_tope_de_consultas_por_turno(respx_mock):
    ctx = make_ctx()
    _con_fuentes(respx_mock, [CATALOGO])
    pedidas = _crm_que_busca(ctx, [_respuesta(found=[MS6])] * (MAX_CONSULTAS_FUENTE + 1))
    llamada = ToolCall(id="f", name="consultar_fuente", arguments={"fuente": "fu_catalogo", "consulta": "metal slug 6"})
    ctx.llm.replies = [LlmReply(content=None, tool_calls=[llamada])] * (MAX_CONSULTAS_FUENTE + 1) + [
        LlmReply(content="Sí viene Metal Slug 6.")
    ]
    await _turno(ctx, "trae metal slug 6?")
    assert len(pedidas) == MAX_CONSULTAS_FUENTE
    ultima = ctx.llm.calls[MAX_CONSULTAS_FUENTE + 1]["messages"][-1]["content"]
    assert "limite_de_consultas" in ultima


async def test_fuente_que_no_existe_no_se_consulta(respx_mock):
    ctx = make_ctx()
    _con_fuentes(respx_mock, [CATALOGO])
    pedidas = _crm_que_busca(ctx, [])
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="f", name="consultar_fuente", arguments={"fuente": "fu_otra", "consulta": "x"})],
        ),
        LlmReply(content="Déjame ver."),
    ]
    await _turno(ctx, "trae x?")
    assert pedidas == []
    assert "fuente_desconocida" in ctx.llm.calls[1]["messages"][-1]["content"]


# ------------------------------------------------------------ el contrato con el CRM


async def test_cliente_cloud_pega_a_la_ruta_del_crm_con_su_tope():
    with respx.mock(base_url="http://crm.test") as m:
        ruta = m.post("/api/brains/fuentes/buscar").mock(
            return_value=httpx.Response(200, json=_respuesta(found=[MS6]))
        )
        crm = BrainsCrmClient("http://crm.test", "secreto", "remiarcade")
        r = await crm.buscar_en_fuente("cv_1", "fu_catalogo", "metal slug 6")
        await crm.aclose()
    assert r["found"] == [MS6]
    import json

    assert json.loads(ruta.calls[0].request.content) == {
        "conversationId": "cv_1",
        "sourceId": "fu_catalogo",
        "query": "metal slug 6",
    }


async def test_cliente_cloud_un_404_es_error_del_crm():
    from app.crm import CrmError

    with respx.mock(base_url="http://crm.test") as m:
        m.post("/api/brains/fuentes/buscar").mock(return_value=httpx.Response(404, json={"error": {"code": "not_found"}}))
        crm = BrainsCrmClient("http://crm.test", "secreto", "remiarcade")
        try:
            await crm.buscar_en_fuente("cv_1", "fu_x", "x")
            raise AssertionError("debió fallar")
        except CrmError:
            pass
        finally:
            await crm.aclose()


async def test_el_ensayo_consulta_de_verdad_y_lo_anota():
    from app.ensayo import CrmDeEnsayo

    class Lectura:
        async def buscar_en_fuente(self, conversation_id, source_id, query):
            return _respuesta(similar=[MS6])

    crm = CrmDeEnsayo({"sources": [CATALOGO]}, Lectura(), [])
    r = await crm.buscar_en_fuente("cv_ensayo_1", "fu_catalogo", "metal slug 7")
    assert r["similar"] == [MS6]
    assert crm.eventos == [
        {"type": "source_lookup", "source": "fu_catalogo", "query": "metal slug 7", "found": 0, "similar": 1}
    ]

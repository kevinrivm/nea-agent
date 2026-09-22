"""Candado de cierre: conversación que no va a ningún lado.

El agente se despide amable UNA vez y después calla ante el relleno; un
mensaje con contenido reabre y los contadores vuelven a cero. El conteo es
determinista (app/stall.py); aquí se fija el detector, el silencio posterior,
la reapertura y la marca en la ficha del CRM.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.config import Settings
from app.crm_brains import BrainsCrmClient
from app.main import create_app
from app.stall import FICHA_CIERRE, es_relleno, racha_vacia, sin_rumbo, trae_contenido
from app.state import utcnow
from tests.conftest import (
    CRM_URL,
    IDENTITY,
    make_ctx,
    make_settings,
    mock_crm_basics,
    wa_body,
)


# ------------------------------------------------------------- detector ---


@pytest.mark.parametrize(
    "texto",
    ["ok", "va", "ajá", "jaja", "jajaja", "jejeje", "👍", "🙏🙏", "  ", "Gracias"],
)
def test_relleno_no_aporta(texto):
    assert es_relleno(texto) is True


@pytest.mark.parametrize(
    "texto",
    ["ok 👍", "va, gracias 🙏", "Muchas gracias!", "okey", "sale pues", "👍🏻", "jaja ok", "✅"],
)
def test_varios_rellenos_juntos_siguen_siendo_relleno(texto):
    """Es lo que el lead contesta a una despedida: no debe reabrirla."""
    assert es_relleno(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "tengo una clínica dental",
        "no me interesa",  # un "no" es una respuesta clarísima, no relleno
        "ahorita no puedo, la otra semana",
        "somos 12",
        "ok pero cuánto cuesta",
        "hola",  # un saludo es jugada legítima, no vacío
        "buenas tardes",
    ],
)
def test_contenido_real_no_es_relleno(texto):
    assert es_relleno(texto) is False


def test_un_mensaje_con_contenido_corta_la_racha():
    assert racha_vacia(["ok", "va", "tengo una taquería", "ok"]) == 1
    assert racha_vacia(["tengo una taquería", "ok", "va", "ajá"]) == 3


def test_sin_rumbo_por_racha_de_vacios():
    assert sin_rumbo(["hola", "ok", "va", "ajá"], "descubrimiento") is True
    # Dos seguidos no bastan: el saludo NO cuenta como vacío.
    assert sin_rumbo(["hola", "ok", "va"], "descubrimiento") is False


def test_sin_rumbo_por_conversacion_larga_sin_avance():
    largos = [f"mensaje con contenido numero {i}" for i in range(14)]
    assert sin_rumbo(largos, "descubrimiento") is True


def test_agendando_nunca_se_cierra_por_el_candado():
    """Ya hay rumbo: el candado no se mete aunque el lead conteste corto."""
    assert sin_rumbo(["ok", "va", "ajá"], "agendando") is False
    assert sin_rumbo([f"m{i}" * 10 for i in range(20)], "cerrada") is False


# ----------------------------------------------------------- en el turno ---


async def _tres_vacios(ctx, client):
    for i, texto in enumerate(("ok", "va", "ajá")):
        await client.post(
            "/webhook", content=wa_body(text=texto, wamid=f"wamid.v{i}")
        )
        await asyncio.sleep(0.35)


async def test_cierra_una_vez_y_despues_calla(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await _tres_vacios(ctx, client)

    # Se despidió en el tercero (uno por mensaje: 3 respuestas).
    assert routes["messages"].call_count == 3
    marcada = (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at
    assert marcada is not None

    # El alertazo de cierre viajó en el turno del cierre, no antes.
    sistemas = [
        m["content"]
        for m in ctx.llm.calls[-1]["messages"]
        if m["role"] == "system"
    ]
    assert any("UNA línea cálida de cierre" in s for s in sistemas)

    # Quedó a la vista del dueño en la ficha, con fecha y hora local.
    ficha = json.loads(routes["ficha"].calls[-1].request.content)["ficha"]
    assert set(ficha) == {FICHA_CIERRE}
    assert datetime.fromisoformat(ficha[FICHA_CIERRE]).utcoffset() is not None

    # Y a partir de aquí, silencio ante el relleno: ni LLM ni envío.
    llamadas_previas = len(ctx.llm.calls)
    await client.post("/webhook", content=wa_body(text="gracias", wamid="wamid.v9"))
    await asyncio.sleep(0.35)
    assert routes["messages"].call_count == 3
    assert len(ctx.llm.calls) == llamadas_previas


async def test_el_lead_que_vuelve_tras_el_enfriamiento_reabre(
    ctx, client, respx_mock
):
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id="cv_test1",
        stalled_at=utcnow()
        - timedelta(hours=ctx.settings.stall_cooldown_hours, minutes=1),
    )

    # Pasado el enfriamiento reabre cualquier mensaje, hasta un "ok".
    await client.post("/webhook", content=wa_body(text="ok", wamid="wamid.r1"))
    await asyncio.sleep(0.35)

    assert routes["messages"].call_count == 1  # volvió a atenderlo
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is None


async def test_el_candado_no_pisa_el_handoff_por_hostilidad(
    ctx, client, respx_mock
):
    """Tres groserías seguidas son cortas y podrían leerse como "relleno": el
    handoff por hostilidad manda, porque el dueño tiene que ver esa conversación."""
    routes = mock_crm_basics(respx_mock)
    for i, texto in enumerate(("son unos rateros", "puro humo", "pinches estafadores")):
        await client.post(
            "/webhook", content=wa_body(text=texto, wamid=f"wamid.h{i}")
        )
        await asyncio.sleep(0.35)

    assert routes["handoff"].call_count == 1
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is None


async def test_no_manda_escribiendo_a_una_conversacion_cerrada(
    ctx, client, respx_mock
):
    """Un "escribiendo…" seguido de silencio es peor que el silencio solo."""
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_test1", stalled_at=utcnow()
    )
    previos = routes["typing"].call_count

    await client.post("/webhook", content=wa_body(text="ok", wamid="wamid.t1"))
    await asyncio.sleep(0.35)

    assert routes["typing"].call_count == previos
    assert routes["messages"].call_count == 0


# ------------------------------------------------------------- reapertura ---


@pytest.mark.parametrize(
    "tipo,texto,esperado",
    [
        ("text", "gracias", False),
        ("text", "ok 👌", False),
        ("text", "", False),
        ("text", "¿cuánto cuesta la limpieza?", True),
        ("text", "sigues ahí?", True),
        ("button", "Sí, me interesa", True),
        ("audio", None, True),  # una nota de voz se asume con contenido
        ("image", None, True),
        ("document", None, True),
        ("sticker", None, False),
        ("unsupported", None, False),
    ],
)
def test_que_reabre_una_conversacion_cerrada(tipo, texto, esperado):
    assert trae_contenido(tipo, texto) is esperado


def test_un_umbral_en_cero_apaga_ese_disparador():
    assert sin_rumbo(["ok"] * 10, "descubrimiento", racha=0) is False
    largos = [f"mensaje con contenido {i}" for i in range(50)]
    assert sin_rumbo(largos, "descubrimiento", max_mensajes=0) is False
    # Y el otro disparador sigue vivo.
    assert sin_rumbo(["ok"] * 3, "descubrimiento", max_mensajes=0) is True


def test_umbrales_y_enfriamiento_por_entorno(monkeypatch):
    for var in ("STALL_MAX_TURNS", "STALL_FILLER_STREAK", "STALL_COOLDOWN_HOURS"):
        monkeypatch.delenv(var, raising=False)
    por_defecto = Settings(_env_file=None)
    assert (
        por_defecto.stall_max_turns,
        por_defecto.stall_filler_streak,
        por_defecto.stall_cooldown_hours,
    ) == (14, 3, 24.0)
    monkeypatch.setenv("STALL_MAX_TURNS", "20")
    monkeypatch.setenv("STALL_FILLER_STREAK", "4")
    monkeypatch.setenv("STALL_COOLDOWN_HOURS", "6")
    ajustado = Settings(_env_file=None)
    assert (
        ajustado.stall_max_turns,
        ajustado.stall_filler_streak,
        ajustado.stall_cooldown_hours,
    ) == (20, 4, 6.0)


async def _cerrada(ctx, hace: timedelta = timedelta(minutes=5)):
    """Una conversación que ya se despidió: fase cerrada y marca puesta."""
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    for texto in ("hola", "ok", "va", "ajá"):
        await ctx.store.add_message(conv.id, "user", texto)
        await ctx.store.add_message(conv.id, "assistant", f"respuesta a {texto}")
    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id="cv_test1",
        phase="cerrada",
        stalled_at=utcnow() - hace,
    )
    return conv


async def _manda(client, texto, wamid):
    await client.post("/webhook", content=wa_body(text=texto, wamid=wamid))
    await asyncio.sleep(0.35)


async def test_tras_el_cierre_el_relleno_calla_y_una_pregunta_reabre(
    ctx, client, respx_mock
):
    routes = mock_crm_basics(respx_mock)
    await _cerrada(ctx)

    await _manda(client, "gracias", "wamid.g1")
    assert routes["messages"].call_count == 0
    assert routes["typing"].call_count == 0
    assert ctx.llm.calls == []

    await _manda(client, "¿cuánto cuesta la limpieza?", "wamid.q1")
    assert routes["messages"].call_count == 1  # volvió a contestar
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is None
    assert conv.phase == "descubrimiento"  # ya no queda en `cerrada`
    # La marca de la ficha se borró (el merge del CRM borra con null).
    fichas = [json.loads(c.request.content)["ficha"] for c in routes["ficha"].calls]
    assert {FICHA_CIERRE: None} in fichas
    # Y el hilo viejo no cuenta: la alerta de cierre no viajó en este turno.
    sistemas = [
        m["content"] for m in ctx.llm.calls[-1]["messages"] if m["role"] == "system"
    ]
    assert not any("UNA línea cálida de cierre" in s for s in sistemas)


async def test_tras_reabrir_el_candado_vuelve_a_contar_desde_cero(
    ctx, client, respx_mock
):
    """Antes, reabrir dejaba la fase en `cerrada` y el candado no volvía a
    dispararse nunca; y sin mover la marca, el hilo viejo lo dispararía en el
    primer turno."""
    routes = mock_crm_basics(respx_mock)
    await _cerrada(ctx)
    await _manda(client, "oye, ¿y dan factura?", "wamid.q1")
    await _manda(client, "ok", "wamid.q2")
    await _manda(client, "va", "wamid.q3")
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is None  # dos de relleno tras reabrir: aún no
    await _manda(client, "ajá", "wamid.q4")
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is not None  # el tercero: se despide otra vez
    assert routes["messages"].call_count == 4
    await _manda(client, "gracias", "wamid.q5")
    assert routes["messages"].call_count == 4  # y vuelve a callar


async def test_el_maximo_de_mensajes_cuenta_desde_la_reapertura(respx_mock):
    ctx = make_ctx(make_settings(stall_max_turns=3))
    routes = mock_crm_basics(respx_mock)
    await _cerrada(ctx)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as client:
        await _manda(client, "tengo una clínica dental", "wamid.m1")
        await _manda(client, "somos tres doctores", "wamid.m2")
        conv = await ctx.store.get_or_create_conversation(IDENTITY)
        assert conv.stalled_at is None  # cuatro viejos + dos nuevos: cuentan dos
        await _manda(client, "atendemos en Querétaro", "wamid.m3")
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is not None  # el tercero desde la reapertura
    assert routes["messages"].call_count == 3
    await ctx.crm.aclose()


async def test_racha_configurable(respx_mock):
    ctx = make_ctx(make_settings(stall_filler_streak=2))
    mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as client:
        await _manda(client, "ok", "wamid.c1")
        conv = await ctx.store.get_or_create_conversation(IDENTITY)
        assert conv.stalled_at is None
        await _manda(client, "va", "wamid.c2")
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is not None
    await ctx.crm.aclose()


@pytest.mark.parametrize(
    "hace,contesta", [(timedelta(minutes=30), False), (timedelta(hours=2), True)]
)
async def test_enfriamiento_configurable(respx_mock, hace, contesta):
    ctx = make_ctx(make_settings(stall_cooldown_hours=1))
    routes = mock_crm_basics(respx_mock)
    await _cerrada(ctx, hace=hace)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as client:
        await _manda(client, "gracias", "wamid.e1")
    assert (routes["messages"].call_count == 1) is contesta
    await ctx.crm.aclose()


async def test_si_la_ficha_falla_el_cierre_ocurre_igual(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    routes["ficha"].mock(return_value=httpx.Response(500, json={}))
    await _tres_vacios(ctx, client)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is not None
    assert routes["messages"].call_count == 3
    assert routes["handoff"].call_count == 0  # nada de handoff `error` por esto


async def test_en_cloud_la_marca_va_por_la_ficha_del_cerebro():
    """Vocero Cloud tiene la misma ficha con el mismo merge (`null` borra)."""
    cliente = BrainsCrmClient(CRM_URL, "secreto-de-prueba", "mi-negocio")
    with respx.mock(assert_all_called=True) as mock:
        ruta = mock.put(f"{CRM_URL}/api/brains/ficha").mock(
            return_value=httpx.Response(200, json={"ficha": {}})
        )
        await cliente.put_ficha("cv_1", {FICHA_CIERRE: None})
    assert json.loads(ruta.calls[0].request.content) == {
        "conversationId": "cv_1",
        "ficha": {FICHA_CIERRE: None},
    }
    await cliente.aclose()


async def test_un_maximo_alto_trae_historial_suficiente_para_contarse(respx_mock):
    """Con STALL_MAX_TURNS=25 no bastan los 40 mensajes de siempre (~20 del
    lead): el turno trae los que hagan falta para poder llegar al tope."""
    ctx = make_ctx(make_settings(stall_max_turns=25))
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    for i in range(24):
        await ctx.store.add_message(conv.id, "user", f"dato del negocio número {i}")
        await ctx.store.add_message(conv.id, "assistant", f"respuesta {i}")
    await ctx.store.update_conversation(conv.id, crm_conversation_id="cv_test1")
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as client:
        await _manda(client, "y otro dato más del negocio", "wamid.x25")
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is not None
    assert routes["messages"].call_count == 1
    await ctx.crm.aclose()

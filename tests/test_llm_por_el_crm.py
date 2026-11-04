"""036 (D6 de vocero-crm) — Junto a Vocero raíz, Nea piensa por el CRM.

Con `LLM_API_KEY` vacía la llave del modelo vive en Ajustes → IA del CRM, y
`POST {CRM}/api/bot/llm/*` (compatible con OpenAI) la pone al reenviar. Nea
entra con `CRM_BOT_API_KEY` en `Authorization: Bearer` y usa el modelo que
dice el perfil (`llm.model`), o `LLM_MODEL` si el perfil no trae.

Con `LLM_API_KEY` puesta no cambia nada: cerebro propio con su proveedor. Y el
modo multi-organización sigue con su `/api/brains/llm` (tests/test_multiorg.py).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app import llm as llm_mod
from app import main
from app.llm import LlmPorElCrm, OpenAiLlm
from app.multiorg import LlmSinOrganizacion
from app.profile import ProfileProvider, profile_from_payload
from app.state import InboundMessage
from app.turn import run_turn
from tests.conftest import CRM_URL, IDENTITY, make_ctx, make_settings, mock_crm_basics

PROXY = f"{CRM_URL}/api/bot/llm/chat/completions"


def _respuesta(texto: str = "¡Hola! ¿A qué se dedica tu negocio?") -> dict:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 0,
        "model": "x",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": texto},
            }
        ],
    }


def _perfil(llm: object) -> dict:
    return {"profile": {"name": "Sofi"}, "kb": "", "resources": [], "llm": llm}


def _cliente(settings, perfil) -> LlmPorElCrm:
    return main._llm_del_arranque(settings, perfil)  # type: ignore[return-value]


@pytest.fixture
def sin_backoff(monkeypatch):
    async def instantaneo(_segundos: float) -> None:
        return None

    monkeypatch.setattr(llm_mod, "asyncio", SimpleNamespace(sleep=instantaneo))


# ── Cuándo piensa por el CRM ──────────────────────────────────────────────


def test_sin_llave_propia_contra_la_raiz_piensa_por_el_crm():
    assert make_settings(llm_api_key="").piensa_por_el_crm
    assert make_settings(llm_api_key="   ").piensa_por_el_crm


def test_con_llave_propia_no():
    assert not make_settings(llm_api_key="sk-propia").piensa_por_el_crm


@pytest.mark.parametrize("organizacion", ["", "mi-negocio"])
def test_en_cloud_nunca(organizacion):
    """Cloud tiene su propio contrato (`/api/brains/llm`), no este."""
    s = make_settings(llm_api_key="", vocero_mode="cloud", crm_organization=organizacion)
    assert not s.piensa_por_el_crm


def test_el_arranque_elige_el_cliente_por_modo():
    assert isinstance(_cliente(make_settings(llm_api_key=""), None), LlmPorElCrm)
    assert isinstance(_cliente(make_settings(llm_api_key="sk-propia"), None), OpenAiLlm)
    multi = make_settings(llm_api_key="", vocero_mode="cloud", crm_organization="")
    assert isinstance(_cliente(multi, None), LlmSinOrganizacion)


def test_whisper_por_el_crm_se_avisa():
    """El proxy reenvía chat: `whisper-1` no oye por ahí."""
    assert make_settings(llm_api_key="").audio_mal_configurado
    assert not make_settings(
        llm_api_key="", llm_transcribe_model="google/gemini-2.5-flash"
    ).audio_mal_configurado


# ── El perfil trae el modelo ──────────────────────────────────────────────


def test_el_perfil_mapea_el_bloque_llm():
    p = profile_from_payload(_perfil({"path": "/api/bot/llm", "model": "z-ai/glm-5.3"}), "Nea")
    assert (p.llm_path, p.llm_model) == ("/api/bot/llm", "z-ai/glm-5.3")


@pytest.mark.parametrize(
    "bloque", [None, {}, {"path": "https://otro.host/x", "model": ""}, "basura"]
)
def test_un_bloque_llm_ausente_o_raro_no_elige_nada(bloque):
    p = profile_from_payload(_perfil(bloque), "Nea")
    assert p.llm_model is None and p.llm_path is None


# ── La llamada ────────────────────────────────────────────────────────────


async def test_apunta_al_proxy_con_la_llave_del_bot_y_el_modelo_del_perfil(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/bot/profile").mock(
        return_value=httpx.Response(
            200, json=_perfil({"path": "/api/bot/llm", "model": "z-ai/glm-5.3"})
        )
    )
    ruta = respx_mock.post(PROXY).mock(return_value=httpx.Response(200, json=_respuesta()))
    ctx = make_ctx(make_settings(llm_api_key="", llm_model="gpt-test"))
    perfil = ProfileProvider(ctx.crm)
    llm = _cliente(ctx.settings, perfil)
    try:
        reply = await llm.complete([{"role": "user", "content": "hola"}])
    finally:
        await ctx.crm.aclose()

    assert reply.content and "Hola" in reply.content
    pedido = ruta.calls[0].request
    assert pedido.headers["authorization"] == f"Bearer {ctx.settings.crm_bot_api_key}"
    cuerpo = json.loads(pedido.content)
    assert cuerpo["model"] == "z-ai/glm-5.3"
    # Los ajustes de velocidad viajan igual: el CRM reenvía el cuerpo tal cual.
    assert cuerpo["reasoning"] == {"effort": "minimal"}
    assert cuerpo["provider"] == {"sort": "throughput"}


async def test_sin_ia_en_el_perfil_usa_LLM_MODEL(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/bot/profile").mock(
        return_value=httpx.Response(200, json=_perfil(None))
    )
    ruta = respx_mock.post(PROXY).mock(return_value=httpx.Response(200, json=_respuesta()))
    ctx = make_ctx(make_settings(llm_api_key="", llm_model="gpt-test"))
    llm = _cliente(ctx.settings, ProfileProvider(ctx.crm))
    try:
        await llm.complete([{"role": "user", "content": "hola"}])
    finally:
        await ctx.crm.aclose()
    assert json.loads(ruta.calls[0].request.content)["model"] == "gpt-test"


async def test_un_cambio_de_modelo_en_el_crm_llega_al_vencer_el_perfil(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/bot/profile").mock(
        side_effect=[
            httpx.Response(200, json=_perfil({"path": "/api/bot/llm", "model": "a/uno"})),
            httpx.Response(200, json=_perfil({"path": "/api/bot/llm", "model": "b/dos"})),
        ]
    )
    ruta = respx_mock.post(PROXY).mock(return_value=httpx.Response(200, json=_respuesta()))
    ctx = make_ctx(make_settings(llm_api_key=""))
    perfil = ProfileProvider(ctx.crm, ttl=0)  # TTL vencido en cada turno
    llm = _cliente(ctx.settings, perfil)
    try:
        await llm.complete([{"role": "user", "content": "hola"}])
        await llm.complete([{"role": "user", "content": "hola"}])
    finally:
        await ctx.crm.aclose()
    modelos = [json.loads(c.request.content)["model"] for c in ruta.calls]
    assert modelos == ["a/uno", "b/dos"]
    assert len(llm._clientes) == 2  # un cliente por modelo, sin reinicio


async def test_con_llave_propia_no_toca_el_crm(respx_mock):
    proveedor = respx_mock.post("https://openrouter.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_respuesta())
    )
    s = make_settings(
        llm_api_key="sk-propia", llm_model="mi/modelo", llm_base_url="https://openrouter.test/v1"
    )
    llm = _cliente(s, None)
    await llm.complete([{"role": "user", "content": "hola"}])
    pedido = proveedor.calls[0].request
    assert pedido.headers["authorization"] == "Bearer sk-propia"
    assert json.loads(pedido.content)["model"] == "mi/modelo"


async def test_la_nota_de_voz_va_por_el_proxy_con_el_modelo_que_oye(respx_mock):
    respx_mock.get(f"{CRM_URL}/api/bot/profile").mock(
        return_value=httpx.Response(
            200, json=_perfil({"path": "/api/bot/llm", "model": "z-ai/glm-5.3"})
        )
    )
    ruta = respx_mock.post(PROXY).mock(
        return_value=httpx.Response(200, json=_respuesta("hola, quiero una cita"))
    )
    ctx = make_ctx(
        make_settings(llm_api_key="", llm_transcribe_model="google/gemini-2.5-flash")
    )
    llm = _cliente(ctx.settings, ProfileProvider(ctx.crm))
    try:
        texto = await llm.transcribe(b"ogg", "audio/ogg")
    finally:
        await ctx.crm.aclose()
    assert texto == "hola, quiero una cita"
    assert json.loads(ruta.calls[0].request.content)["model"] == "google/gemini-2.5-flash"


async def test_sin_elegir_modelo_la_nota_de_voz_la_oye_uno_que_oye(respx_mock):
    """Con `whisper-1` (el default) por el CRM, toda nota de voz fallaba."""
    from app.config import MODELO_QUE_OYE

    respx_mock.get(f"{CRM_URL}/api/bot/profile").mock(
        return_value=httpx.Response(
            200, json=_perfil({"path": "/api/bot/llm", "model": "z-ai/glm-5.3"})
        )
    )
    ruta = respx_mock.post(PROXY).mock(
        return_value=httpx.Response(200, json=_respuesta("precio del curso"))
    )
    ctx = make_ctx(make_settings(llm_api_key=""))
    assert ctx.settings.llm_transcribe_model == "whisper-1"
    llm = _cliente(ctx.settings, ProfileProvider(ctx.crm))
    try:
        texto = await llm.transcribe(b"ogg", "audio/ogg")
    finally:
        await ctx.crm.aclose()
    assert texto == "precio del curso"
    enviado = json.loads(ruta.calls[0].request.content)
    assert enviado["model"] == MODELO_QUE_OYE
    assert any(p["type"] == "input_audio" for p in enviado["messages"][0]["content"])


# ── El CRM sin IA: el camino de fallo de siempre ──────────────────────────


async def test_402_sin_credencial_termina_en_silencio_y_handoff_error(sin_backoff, respx_mock):
    """Sin IA configurada (o pausada) el proxy contesta 402: el turno degrada
    como con cualquier fallo del proveedor. Nada roto le llega al lead."""
    rutas = mock_crm_basics(respx_mock)
    respx_mock.get(f"{CRM_URL}/api/bot/profile").mock(
        return_value=httpx.Response(200, json=_perfil(None))
    )
    proxy = respx_mock.post(PROXY).mock(
        return_value=httpx.Response(402, json={"error": {"code": "sin_credencial"}})
    )
    ctx = make_ctx(make_settings(llm_api_key=""))
    ctx.profile = ProfileProvider(ctx.crm)
    ctx.llm = _cliente(ctx.settings, ctx.profile)
    try:
        await run_turn(
            ctx,
            IDENTITY,
            [InboundMessage(wa_message_id="wamid.402", identity=IDENTITY, type="text", text="hola")],
        )
    finally:
        await ctx.crm.aclose()

    assert proxy.call_count == 1 + OpenAiLlm.RETRIES
    assert rutas["messages"].call_count == 0
    assert rutas["handoff"].call_count == 1
    assert json.loads(rutas["handoff"].calls[0].request.content)["reason"] == "error"
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.phase == "cerrada"


# ── El arranque y /health ─────────────────────────────────────────────────


class _Store:
    async def connect(self) -> None:
        return None

    async def migrate(self, _directorio) -> None:
        return None

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _WorkerQuieto:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()


async def test_arranca_sin_LLM_API_KEY_y_dice_crm_en_health(monkeypatch, respx_mock):
    monkeypatch.setenv("VOCERO_MODE", "")
    monkeypatch.setenv("CRM_BASE_URL", CRM_URL)
    monkeypatch.setenv("CRM_BOT_API_KEY", "llave-del-bot-de-prueba")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "postgres://no-se-usa")
    monkeypatch.setattr(main, "PgStore", lambda *_a, **_k: _Store())
    monkeypatch.setattr(main, "run_dispatch_worker", lambda _c: _WorkerQuieto().run())
    for worker in ("FollowupWorker", "SenderWorker"):
        monkeypatch.setattr(main, worker, _WorkerQuieto)
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(422, json={"error": {"code": "invalid_body"}})
    )

    app = main.create_app()
    async with app.router.lifespan_context(app):
        c = app.state.ctx
        assert isinstance(c.llm, LlmPorElCrm)
        assert c.llm._perfil is c.profile  # el mismo perfil (y su caché) del turno
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
        ) as cliente:
            resp = await cliente.get("/health")
    cuerpo = resp.json()
    assert resp.status_code == 200
    assert cuerpo["llm"] == "crm"
    assert "llave-del-bot-de-prueba" not in resp.text
    assert CRM_URL not in resp.text


@pytest.mark.parametrize(
    "ajustes,esperado",
    [
        ({"llm_api_key": "sk-propia"}, "propia"),
        ({"llm_api_key": ""}, "crm"),
        ({"llm_api_key": "", "vocero_mode": "cloud", "crm_organization": ""}, "crm"),
        ({"llm_api_key": "sk-x", "vocero_mode": "cloud", "crm_organization": "uno"}, "propia"),
    ],
)
def test_health_dice_con_que_llave_piensa(ajustes, esperado):
    assert main._identidad(make_settings(**ajustes))["llm"] == esperado

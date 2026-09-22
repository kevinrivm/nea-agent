"""Un proveedor del modelo colgado ya no deja al lead media hora esperando.

El cliente de OpenAI se creaba sin tope: el SDK espera hasta 600 s por
petición y reintenta DOS veces más por su cuenta, encima de los reintentos de
Nea. Con un proveedor que acepta la conexión y no contesta, cada intento de
`complete` eran hasta tres peticiones de diez minutos antes de llegar al
handoff `error`.

Ahora cada intento tiene tope (`LLM_TIMEOUT_SECONDS`, 45 s por defecto) y el
SDK no reintenta: los reintentos son los de Nea y terminan en el mismo camino
de siempre — `LlmExhausted` → silencio + handoff `error`.

Varias pruebas usan un servidor TCP local que acepta y nunca contesta, en vez
de un mock: un mock de respx no respeta timeouts, y lo que hay que probar es
justo que el tope llega hasta la capa HTTP.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app import llm as llm_mod
from app import main
from app.config import Settings
from app.llm import LlmExhausted, OpenAiLlm
from app.multiorg import RegistroDeOrganizaciones
from app.state import InboundMessage
from app.turn import run_turn
from tests.conftest import CRM_URL, IDENTITY, make_ctx, mock_crm_basics

SECRETO = "secreto-del-despliegue-de-32-caracteres"


class _ProveedorColgado:
    """Acepta la conexión, lee la petición y no contesta nunca."""

    def __init__(self) -> None:
        self.conexiones = 0
        self._soltar = asyncio.Event()

    async def _atender(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.conexiones += 1
        try:
            await reader.read(65536)
            await self._soltar.wait()
        finally:
            writer.close()

    async def __aenter__(self) -> "_ProveedorColgado":
        self._server = await asyncio.start_server(self._atender, "127.0.0.1", 0)
        puerto = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{puerto}/v1"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._soltar.set()
        self._server.close()
        await asyncio.wait_for(self._server.wait_closed(), timeout=5)


@pytest.fixture
def sin_backoff(monkeypatch):
    """Los reintentos de Nea esperan 1 s y 2 s; aquí se prueba que ocurren,
    no cuánto tardan."""

    async def instantaneo(_segundos: float) -> None:
        return None

    monkeypatch.setattr(llm_mod, "asyncio", SimpleNamespace(sleep=instantaneo))


# ── La configuración ──────────────────────────────────────────────────────


def test_por_defecto_45_s_por_intento():
    assert Settings(_env_file=None).llm_timeout_seconds == 45.0


def test_se_configura_con_LLM_TIMEOUT_SECONDS(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")
    assert Settings(_env_file=None).llm_timeout_seconds == 12.5


@pytest.mark.parametrize("valor", ["0", "-3"])
def test_un_tope_sin_sentido_no_arranca(monkeypatch, valor):
    """Cero o negativo haría fallar TODA llamada: mejor que no arranque."""
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", valor)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_el_cliente_lleva_el_tope_y_el_sdk_no_reintenta():
    cliente = OpenAiLlm("k", "m", timeout=7.0)._client
    assert cliente.timeout == 7.0
    assert cliente.max_retries == 0


def test_sin_tope_explicito_tampoco_queda_el_del_sdk():
    """Quien construya el cliente a mano (bench, selftest) no hereda los
    600 s del SDK."""
    cliente = OpenAiLlm("k", "m")._client
    assert cliente.timeout == 45.0
    assert cliente.max_retries == 0


def test_en_multiorg_el_tope_es_el_de_esta_nea():
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    cliente = reg.llm("org_1", "uno", {"model": "a/b"}, Settings(_env_file=None, llm_timeout_seconds=9))
    assert cliente._client.timeout == 9
    assert cliente._client.max_retries == 0


class _StoreDePrueba:
    async def connect(self) -> None:
        return None

    async def migrate(self, _directorio) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _WorkerQuieto:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        return None


async def test_el_arranque_de_siempre_arma_el_llm_con_el_tope(monkeypatch, respx_mock):
    monkeypatch.setenv("VOCERO_MODE", "")
    monkeypatch.setenv("CRM_BASE_URL", CRM_URL)
    monkeypatch.setenv("LLM_API_KEY", "sk-prueba")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("DATABASE_URL", "postgres://no-se-usa")
    monkeypatch.setattr(main, "PgStore", lambda *_a, **_k: _StoreDePrueba())
    for worker in ("RelayWorker", "FollowupWorker", "SenderWorker"):
        monkeypatch.setattr(main, worker, _WorkerQuieto)
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(422, json={"error": {"code": "invalid_body"}})
    )

    app = main.create_app()
    async with app.router.lifespan_context(app):
        cliente = app.state.ctx.llm._client
        assert cliente.timeout == 20.0
        assert cliente.max_retries == 0


# ── El tope de verdad, contra un proveedor colgado ────────────────────────


async def test_un_proveedor_colgado_se_corta_sin_reintentos_del_sdk(sin_backoff):
    async with _ProveedorColgado() as proveedor:
        llm = OpenAiLlm("k", "m", base_url=proveedor.url, timeout=0.3)
        inicio = time.monotonic()
        with pytest.raises(LlmExhausted):
            await llm.complete([{"role": "user", "content": "hola"}])
        tardo = time.monotonic() - inicio

    # 1 intento + RETRIES de Nea, y ni uno más: con los 2 del SDK serían 9.
    assert proveedor.conexiones == 1 + OpenAiLlm.RETRIES
    assert tardo < 5


async def test_transcribir_por_el_chat_tambien_se_corta(sin_backoff):
    async with _ProveedorColgado() as proveedor:
        llm = OpenAiLlm(
            "k", "m", transcribe_model="oye", base_url=proveedor.url, timeout=0.3
        )
        with pytest.raises(LlmExhausted):
            await llm.transcribe(b"OggS...", "audio/ogg")

    assert proveedor.conexiones == 2  # los dos intentos de Nea, nada del SDK


async def test_whisper_tampoco_multiplica_los_intentos(sin_backoff, respx_mock):
    ruta = respx_mock.post("https://api.openai.com/v1/audio/transcriptions").mock(
        side_effect=httpx.ReadTimeout("el proveedor no contesta")
    )
    llm = OpenAiLlm("k", "m", transcribe_model="whisper-1")
    with pytest.raises(LlmExhausted):
        await llm.transcribe(b"OggS...", "audio/ogg")
    assert ruta.call_count == 2  # con los reintentos del SDK eran 6


async def test_un_timeout_en_el_turno_termina_en_silencio_y_handoff_error(
    sin_backoff, respx_mock
):
    """El camino completo: turno real, proveedor colgado, CRM en respx."""
    rutas = mock_crm_basics(respx_mock)
    respx_mock.route(host="127.0.0.1").pass_through()
    async with _ProveedorColgado() as proveedor:
        ctx = make_ctx(llm=OpenAiLlm("k", "m", base_url=proveedor.url, timeout=0.3))
        try:
            await run_turn(
                ctx,
                IDENTITY,
                [InboundMessage(wa_message_id="wamid.t1", identity=IDENTITY, type="text", text="hola")],
            )
        finally:
            await ctx.crm.aclose()

    assert proveedor.conexiones == 1 + OpenAiLlm.RETRIES
    assert rutas["messages"].call_count == 0  # nada roto al lead
    assert rutas["handoff"].call_count == 1
    assert json.loads(rutas["handoff"].calls[0].request.content)["reason"] == "error"
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.phase == "cerrada"

"""La agenda del CRM se vuelve a preguntar: encenderla ya no exige reiniciar.

Antes se sondeaba UNA vez al arrancar, y el 404 de una herramienta la apagaba
para todo el proceso. Ahora la respuesta caduca (`AGENDA_PROBE_TTL_SECONDS`):
a lo sumo una sonda por TTL, con timeout corto, y si no concluye se queda lo
último que se supo. Qué 404 cuenta como «apagada» está en test_agenda_404.py.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest

from app import main
from app.agenda import SONDA_TIMEOUT, SondaDeAgenda, agenda_vigente
from app.config import Settings
from app.crm import CrmError
from app.multiorg import CrmSinOrganizacion
from app.state import InboundMessage, OfferedSlot
from app.tools import ToolRuntime
from app.turn import run_turn
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx, mock_crm_basics

SLOT_ISO = "2026-07-20T16:00:00Z"
SLOT_DT = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)


class _Reloj:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def avanza(self, segundos: float) -> None:
        self.t += segundos


class _CrmDeSonda:
    """Contesta la sonda con lo que le digan, y cuenta."""

    def __init__(self, *respuestas: object) -> None:
        self.respuestas = list(respuestas)
        self.timeouts: list[float | None] = []

    @property
    def preguntas(self) -> int:
        return len(self.timeouts)

    async def sondear_agenda(self, timeout: float | None = None) -> bool | None:
        self.timeouts.append(timeout)
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, BaseException):
            raise r
        return r  # type: ignore[return-value]


# ── La sonda ──────────────────────────────────────────────────────────────


async def test_se_pregunta_a_lo_sumo_una_vez_por_ttl():
    crm, reloj = _CrmDeSonda(True), _Reloj()
    sonda = SondaDeAgenda(crm, ttl=60, reloj=reloj)

    assert await sonda.vigente() is True
    reloj.avanza(59)
    assert await sonda.vigente() is True
    assert crm.preguntas == 1
    reloj.avanza(2)
    await sonda.vigente()
    assert crm.preguntas == 2


async def test_encender_la_agenda_en_el_crm_llega_sin_reiniciar():
    crm, reloj = _CrmDeSonda(False, True), _Reloj()
    sonda = SondaDeAgenda(crm, ttl=60, reloj=reloj)

    assert await sonda.vigente() is False
    reloj.avanza(61)
    assert await sonda.vigente() is True


@pytest.mark.parametrize("falla", [None, CrmError("sin red"), RuntimeError("raro")])
@pytest.mark.parametrize("antes", [True, False])
async def test_una_sonda_que_no_concluye_deja_lo_ultimo_que_se_supo(antes, falla):
    crm, reloj = _CrmDeSonda(antes, falla), _Reloj()
    sonda = SondaDeAgenda(crm, ttl=60, reloj=reloj)
    assert await sonda.vigente() is antes

    reloj.avanza(61)
    assert await sonda.vigente() is antes
    # Y no se martillea al CRM caído: la siguiente, hasta el otro TTL.
    reloj.avanza(30)
    await sonda.vigente()
    assert crm.preguntas == 2


async def test_sin_nada_que_saber_se_asume_que_si():
    """Al arrancar con el CRM caído: equivocarse hacia el sí cuesta un intento
    fallido; hacia el no apagaría una agenda que existe."""
    sonda = SondaDeAgenda(_CrmDeSonda(None), ttl=60, reloj=_Reloj())
    assert await sonda.vigente() is True


async def test_varios_turnos_a_la_vez_disparan_una_sola_sonda():
    soltar = asyncio.Event()

    class _CrmLento(_CrmDeSonda):
        async def sondear_agenda(self, timeout=None):
            await soltar.wait()
            return await super().sondear_agenda(timeout)

    crm = _CrmLento(False)
    sonda = SondaDeAgenda(crm, ttl=60, reloj=_Reloj())
    turnos = [asyncio.create_task(sonda.vigente()) for _ in range(5)]
    await asyncio.sleep(0.05)
    soltar.set()

    assert await asyncio.gather(*turnos) == [False] * 5
    assert crm.preguntas == 1


async def test_la_sonda_no_retiene_al_turno_mas_que_su_timeout():
    class _CrmColgado:
        async def sondear_agenda(self, timeout=None):
            await asyncio.sleep(30)
            return False

    sonda = SondaDeAgenda(_CrmColgado(), ttl=60, timeout=0.1, reloj=_Reloj())
    inicio = time.monotonic()
    assert await sonda.vigente() is True  # lo último que se sabía
    assert time.monotonic() - inicio < 2


async def test_la_sonda_pide_un_timeout_corto():
    crm = _CrmDeSonda(True)
    await SondaDeAgenda(crm, ttl=60, reloj=_Reloj()).vigente()
    assert crm.timeouts == [SONDA_TIMEOUT]
    assert SONDA_TIMEOUT <= 5


async def test_el_404_de_una_herramienta_apaga_solo_hasta_el_ttl():
    crm, reloj = _CrmDeSonda(True), _Reloj()
    sonda = SondaDeAgenda(crm, ttl=60, reloj=reloj)
    await sonda.vigente()

    sonda.marcar_apagada()
    assert await sonda.vigente() is False
    reloj.avanza(59)
    assert await sonda.vigente() is False
    assert crm.preguntas == 1  # marcarla no gasta sonda

    reloj.avanza(2)
    assert await sonda.vigente() is True  # se volvió a preguntar
    assert crm.preguntas == 2


async def test_una_sonda_vieja_no_pisa_un_404_mas_reciente():
    soltar = asyncio.Event()

    class _CrmLento(_CrmDeSonda):
        async def sondear_agenda(self, timeout=None):
            await soltar.wait()
            return await super().sondear_agenda(timeout)

    sonda = SondaDeAgenda(_CrmLento(True), ttl=60, reloj=_Reloj())
    en_vuelo = asyncio.create_task(sonda.vigente())
    await asyncio.sleep(0.05)
    sonda.marcar_apagada()  # el 404 llegó mientras la sonda iba en camino
    soltar.set()

    assert await en_vuelo is False
    assert sonda.valor is False


async def test_ttl_cero_pregunta_en_cada_turno():
    crm = _CrmDeSonda(True)
    sonda = SondaDeAgenda(crm, ttl=0, reloj=_Reloj())
    for _ in range(3):
        await sonda.vigente()
    assert crm.preguntas == 3


async def test_un_cliente_sin_sonda_nueva_usa_la_pregunta_de_siempre():
    class _CrmViejo:
        async def agenda_available(self) -> bool:
            return False

    assert await SondaDeAgenda(_CrmViejo(), ttl=60, reloj=_Reloj()).vigente() is False


async def test_el_centinela_multiorg_no_apaga_para_siempre():
    """Con None la sonda se quedaría con «apagada» tras un 404; el centinela
    dice que sí y el siguiente turno vuelve a intentar con su credencial."""
    reloj = _Reloj()
    sonda = SondaDeAgenda(CrmSinOrganizacion(), ttl=60, reloj=reloj)
    await sonda.vigente()
    sonda.marcar_apagada()
    reloj.avanza(61)
    assert await sonda.vigente() is True


async def test_sin_sonda_manda_lo_fijado_en_el_contexto():
    ctx = make_ctx()
    ctx.agenda_enabled = False
    assert await agenda_vigente(ctx) is False
    await ctx.crm.aclose()


def test_el_ttl_se_configura():
    assert Settings(_env_file=None).agenda_probe_ttl_seconds == 60.0
    assert Settings(_env_file=None, agenda_probe_ttl_seconds=5).agenda_probe_ttl_seconds == 5
    with pytest.raises(ValueError):
        Settings(_env_file=None, agenda_probe_ttl_seconds=-1)


# ── Las herramientas: el 404 de la bandera caduca ───────────────────────


@pytest.fixture
async def runtime_con_sonda():
    ctx = make_ctx()
    reloj = _Reloj()
    ctx.agenda_sonda = SondaDeAgenda(ctx.crm, ttl=60, reloj=reloj)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.replace_offered_slots(
        conv.id,
        [OfferedSlot(conv.id, SLOT_DT, None, "lunes 20 de julio, 10:00 am")],
    )
    yield ToolRuntime(ctx, conv, CRM_CONV_ID), ctx, reloj
    await ctx.crm.aclose()


async def test_el_404_de_la_bandera_apaga_hasta_la_siguiente_sonda(
    runtime_con_sonda, respx_mock
):
    runtime, ctx, reloj = runtime_con_sonda
    ruta = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(404)
    )
    result = await runtime.execute("propose_slots", {})
    assert result["error"] == "sin_agenda"
    assert ctx.agenda_enabled is False
    assert await agenda_vigente(ctx) is False  # dentro del TTL, sin sondear

    ruta.mock(return_value=httpx.Response(422, json={}))  # la encendieron
    reloj.avanza(61)
    assert await agenda_vigente(ctx) is True


# ── De punta a punta: el turno ────────────────────────────────────────────


def _hay_herramienta_de_agenda(llamada: dict) -> bool:
    return any(t["function"]["name"] == "propose_slots" for t in llamada["tools"] or [])


async def test_encender_agenda_en_el_crm_llega_al_turno_sin_reiniciar(respx_mock):
    rutas = mock_crm_basics(respx_mock)
    sonda_crm = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(404)  # AGENDA apagada
    )
    ctx = make_ctx()
    reloj = _Reloj()
    ctx.agenda_sonda = SondaDeAgenda(ctx.crm, ttl=60, reloj=reloj)
    ctx.agenda_enabled = await ctx.agenda_sonda.vigente()  # el arranque
    assert ctx.agenda_enabled is False

    async def turno(texto: str, wamid: str) -> dict:
        await run_turn(
            ctx, IDENTITY, [InboundMessage(wa_message_id=wamid, identity=IDENTITY, type="text", text=texto)]
        )
        return ctx.llm.calls[-1]

    try:
        antes = await turno("tengo una clínica dental en León", "wamid.a1")
        assert not _hay_herramienta_de_agenda(antes)
        assert "ESTE NEGOCIO NO AGENDA" in antes["messages"][0]["content"]

        sonda_crm.mock(return_value=httpx.Response(422, json={}))  # AGENDA=on
        reloj.avanza(30)
        dentro_del_ttl = await turno("somos tres doctores", "wamid.a2")
        assert not _hay_herramienta_de_agenda(dentro_del_ttl)
        assert sonda_crm.call_count == 1  # el TTL se respeta

        reloj.avanza(31)
        despues = await turno("¿me das una cita para el jueves?", "wamid.a3")
        assert _hay_herramienta_de_agenda(despues)
        assert "ESTE NEGOCIO NO AGENDA" not in despues["messages"][0]["content"]
        assert sonda_crm.call_count == 2
    finally:
        await ctx.crm.aclose()
    assert rutas["messages"].call_count == 3


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


async def test_el_arranque_deja_armada_la_sonda(monkeypatch, respx_mock):
    monkeypatch.setenv("VOCERO_MODE", "")
    monkeypatch.setenv("CRM_BASE_URL", CRM_URL)
    monkeypatch.setenv("LLM_API_KEY", "sk-prueba")
    monkeypatch.setenv("AGENDA_PROBE_TTL_SECONDS", "15")
    monkeypatch.setenv("DATABASE_URL", "postgres://no-se-usa")
    monkeypatch.setattr(main, "PgStore", lambda *_a, **_k: _StoreDePrueba())
    for worker in ("RelayWorker", "FollowupWorker", "SenderWorker"):
        monkeypatch.setattr(main, worker, _WorkerQuieto)
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(return_value=httpx.Response(404))

    app = main.create_app()
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx
        assert ctx.agenda_enabled is False
        assert isinstance(ctx.agenda_sonda, SondaDeAgenda)
        assert ctx.agenda_sonda.ttl == 15

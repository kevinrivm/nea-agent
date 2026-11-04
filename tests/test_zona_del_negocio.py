"""La zona horaria es la del negocio que se atiende, no la de la instancia.

Producción, AIMA, 13 oct 2026. Negocio en America/Bogota, atendido por una Nea
con AGENT_TIMEZONE=America/Mexico_City para todos sus negocios. El CRM ofreció
bien —las 14:00 de Bogotá— y el agente remató: «es de México el huso, así que
serían de la tarde tu hora». El prompt le decía «(America/Mexico_City)» junto
a la hora, una hora atrasada además.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.followup import FollowupWorker
from app.llm import LlmReply
from app.profile import BusinessProfile
from app.prompt import build_system_prompt, hora_de, zona_del_negocio
from app.state import Conversation
from app.tools import ToolRuntime
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    crm_context,
    make_ctx,
    mock_crm_basics,
    despachar,
)
from tests.test_followup import preparar_conversacion

BOGOTA = ZoneInfo("America/Bogota")
CDMX = ZoneInfo("America/Mexico_City")


def _contexto_de(zona: str | None) -> dict:
    contexto = crm_context()
    contexto["booking"] = {"timezone": zona, "next": None, "unresolved": None}
    return contexto


def _bloque(now: datetime, tz: ZoneInfo, agenda: bool = True) -> str:
    prompt = build_system_prompt(
        profile=BusinessProfile(),
        context=_contexto_de(tz.key),
        conv=Conversation(id=1, wa_identity=IDENTITY, greeted=True),
        agenda=agenda,
        now=now,
        tz=tz,
    )
    return prompt.split("CONTEXTO ACTUAL:", 1)[1]


# ── De dónde sale la zona ─────────────────────────────────────────────────


def test_la_zona_es_la_que_el_crm_manda_con_las_citas():
    assert zona_del_negocio(_contexto_de("America/Bogota"), CDMX) == BOGOTA


@pytest.mark.parametrize(
    "contexto",
    [
        None,
        {},
        {"booking": None},  # agenda apagada: el CRM no manda el bloque
        {"booking": {"next": None}},  # CRM de antes de la zona en el bloque
        {"booking": {"timezone": "Marte/Olympus_Mons"}},
        {"booking": {"timezone": ""}},
    ],
)
def test_sin_zona_del_crm_queda_la_de_la_instancia(contexto):
    assert zona_del_negocio(contexto, BOGOTA) == BOGOTA


def test_sin_zona_ni_respaldo_queda_la_de_siempre():
    assert zona_del_negocio(None) == CDMX


# ── Lo que lee el modelo ──────────────────────────────────────────────────


def test_el_prompt_da_la_hora_del_negocio_y_no_otra():
    # Las 18:42 de Bogotá; en CDMX eran las 17:42.
    ahora = datetime(2026, 10, 13, 23, 42, tzinfo=timezone.utc)
    bloque = _bloque(ahora, BOGOTA)
    assert (
        "Fecha y hora del negocio: martes 13 de octubre de 2026, 18:42 (hora de Colombia)"
        in bloque
    )
    assert "México" not in bloque


def test_el_nombre_tecnico_de_la_zona_no_entra_al_prompt():
    # Con «America/Bogota» a la vista, en la autoprueba con el modelo real se
    # le escapó al lead: «(la hora del negocio, America/Bogota)».
    ahora = datetime(2026, 10, 13, 23, 42, tzinfo=timezone.utc)
    for tz in (BOGOTA, CDMX, ZoneInfo("Europe/Lisbon")):
        assert "/" not in hora_de(tz)
        assert tz.key not in _bloque(ahora, tz)


def test_una_zona_que_no_esta_en_la_lista_se_dice_por_su_ciudad():
    assert hora_de(ZoneInfo("America/Argentina/Cordoba")) == "hora de Cordoba"
    assert hora_de(ZoneInfo("Europe/Lisbon")) == "hora de Lisbon"


def test_la_zona_de_siempre_se_dice_como_se_dice_en_mexico():
    assert hora_de(CDMX) == "hora del centro de México"


def test_los_horarios_se_dicen_en_la_hora_del_negocio_sin_convertir():
    ahora = datetime(2026, 10, 13, 23, 42, tzinfo=timezone.utc)
    bloque = _bloque(ahora, BOGOTA)
    assert "Los horarios de la agenda van en esa hora, la del negocio" in bloque
    assert "No supongas en qué país u hora está el lead" in bloque
    assert "dile que es la hora de Colombia" in bloque
    # Sin el «solo», la mitad de las veces lo decía en cada oferta.
    assert "sin aclarar de dónde son" in bloque and "Solo si pregunta" in bloque
    # En la autoprueba con el modelo real, sin esto convertía por su cuenta:
    # «las 14:00 de México son las 22:00 de Madrid» (eran las 21:00).
    assert "NO le calcules cuánto sería en su hora" in bloque


def test_hoy_y_manana_son_los_del_negocio():
    # 00:30 del miércoles 14 en Bogotá; en CDMX seguía siendo martes 13.
    ahora = datetime(2026, 10, 14, 5, 30, tzinfo=timezone.utc)
    bloque = _bloque(ahora, BOGOTA)
    assert '"Hoy" es miércoles 14 de octubre de 2026' in bloque
    assert '"mañana" es jueves 15 de octubre de 2026' in bloque
    assert "hoy miércoles 14 = 2026-10-14" in bloque


def test_sin_agenda_no_se_habla_de_horarios():
    ahora = datetime(2026, 10, 13, 23, 42, tzinfo=timezone.utc)
    assert "Los horarios de la agenda" not in _bloque(ahora, BOGOTA, agenda=False)


# ── El turno y el seguimiento usan esa zona ───────────────────────────────


async def test_el_turno_arma_el_prompt_con_la_zona_del_negocio(ctx, client, respx_mock):
    assert ctx.settings.agent_timezone == "America/Mexico_City"
    routes = mock_crm_basics(respx_mock)
    routes["context"].mock(
        return_value=httpx.Response(200, json=_contexto_de("America/Bogota"))
    )
    await despachar(client, ctx, text="¿tienes en la mañana?")
    await asyncio.sleep(0.25)

    system = ctx.llm.calls[0]["messages"][0]["content"]
    assert "(hora de Colombia)" in system
    assert "centro de México" not in system


async def test_sin_zona_del_crm_el_turno_usa_la_de_la_instancia(ctx, client, respx_mock):
    mock_crm_basics(respx_mock)  # su bloque `booking` no trae zona
    await despachar(client, ctx, text="hola")
    await asyncio.sleep(0.25)

    system = ctx.llm.calls[0]["messages"][0]["content"]
    assert "(hora del centro de México)" in system


async def test_el_seguimiento_tambien_usa_la_zona_del_negocio(respx_mock):
    ctx = make_ctx()
    ctx.llm.replies = [LlmReply(content="¿Seguimos donde nos quedamos?")]
    await preparar_conversacion(ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_contexto_de("America/Bogota"))
    )
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    await FollowupWorker(ctx).tick()

    system = ctx.llm.calls[0]["messages"][0]["content"]
    assert "(hora de Colombia)" in system
    assert "centro de México" not in system
    await ctx.crm.aclose()


# ── Las herramientas también ──────────────────────────────────────────────


async def test_el_dia_de_un_hueco_se_cuenta_en_la_zona_del_negocio(respx_mock):
    """Un CRM que ignora `date` y no manda `dayIso`: el día lo deduce Nea.

    05:30 UTC del 15 son las 07:30 del 15 en Madrid y las 23:30 del 14 en
    CDMX. Para un negocio de Madrid ese hueco ES del día 15.
    """
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": [
                    {
                        "startUtc": "2026-10-15T05:30:00.000Z",
                        "label": "jue 15 oct, 07:30",
                        "dayLabel": "jueves 15 de octubre",
                        "time": "07:30",
                    }
                ]
            },
        )
    )
    madrid = ToolRuntime(ctx, conv, CRM_CONV_ID, tz=ZoneInfo("Europe/Madrid"))
    result = await madrid.execute("propose_slots", {"fecha": "2026-10-15"})
    assert result["horas_que_veo_de_ese_dia"] == ["07:30"]

    de_la_instancia = ToolRuntime(ctx, conv, CRM_CONV_ID)
    result = await de_la_instancia.execute("propose_slots", {"fecha": "2026-10-15"})
    assert result["horas_que_veo_de_ese_dia"] == []
    await ctx.crm.aclose()

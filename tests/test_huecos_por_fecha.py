"""propose_slots con fecha, y lo que el agente puede afirmar de lo que no ve.

Fija los fallos de Tobaxis (9 y 10 sep 2026): con el reparto de 3 horas por
día, el agente dijo "la próxima semana jueves o viernes ya no tienen agenda"
(días que no se consultaron) y "el jueves a las 11 no hay espacio" (solo veía
las 3 primeras horas del día).
"""
from __future__ import annotations

import httpx
import pytest

from app.tools import MAX_OFFERED_DIA, ToolRuntime, tool_schemas
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

AVAILABILITY = f"{CRM_URL}/api/bot/availability"


def _horas(dia: str, n: int, paso: int = 30) -> list[dict[str, str]]:
    """n turnos cada `paso` min desde las 9:00 CDMX (15:00 UTC)."""
    out = []
    for i in range(n):
        h, m = divmod(15 * 60 + paso * i, 60)
        out.append(
            {
                "startUtc": f"{dia}T{h:02d}:{m:02d}:00.000Z",
                "label": "x",
                "dayLabel": f"jueves {dia}",
                "time": f"{h - 6:02d}:{m:02d}",
            }
        )
    return out


@pytest.fixture
async def runtime_y_ctx():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID)
    yield runtime, ctx, conv
    await ctx.crm.aclose()


def test_propose_slots_acepta_fecha():
    schema = next(t for t in tool_schemas() if t["function"]["name"] == "propose_slots")
    assert "fecha" in schema["function"]["parameters"]["properties"]


async def test_el_reparto_ya_no_dice_que_lo_ausente_no_tiene_agenda(
    runtime_y_ctx, respx_mock
):
    runtime, _, _ = runtime_y_ctx
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": _horas("2026-09-10", 3),
                "query": {"date": None, "status": None, "coveredUntil": "2026-09-15", "horizonEnd": "2026-09-23", "perDay": 3},
            },
        )
    )
    result = await runtime.execute("propose_slots", {})
    texto = result["instrucciones"]
    assert "TODA la agenda abierta" not in texto
    assert "fecha=AAAA-MM-DD" in texto
    assert "2026-09-15 NO se revisaron" in texto
    assert "hasta 3 horas" in texto
    assert result["revisado_hasta"] == "2026-09-15"


async def test_contra_un_crm_viejo_la_lista_tambien_es_parcial(
    runtime_y_ctx, respx_mock
):
    runtime, _, _ = runtime_y_ctx
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(200, json={"slots": _horas("2026-09-10", 3)})
    )
    result = await runtime.execute("propose_slots", {})
    assert "Nunca digas que un día u hora no tiene agenda" in result["instrucciones"]


async def test_con_fecha_trae_todas_las_horas_y_las_deja_reservables(
    runtime_y_ctx, respx_mock
):
    runtime, ctx, conv = runtime_y_ctx
    route = respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": _horas("2026-09-10", 16),
                "query": {"date": "2026-09-10", "status": "available", "coveredUntil": "2026-09-10", "horizonEnd": "2026-09-23", "perDay": None},
            },
        )
    )
    result = await runtime.execute("propose_slots", {"fecha": "2026-09-10"})
    assert route.calls[0].request.url.params["date"] == "2026-09-10"
    assert result["ok"] is True
    assert len(result["slots"]) == 16
    assert any(s["label"].endswith("11:00") for s in result["slots"])
    assert "horas libres del 2026-09-10" in result["instrucciones"]
    # Las 16 son reservables: el espejo no puede recortar lo que el CRM ofreció.
    assert len(await ctx.store.get_offered_slots(conv.id)) == 16


async def test_el_espejo_guarda_hasta_el_tope_del_dia(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": _horas("2026-09-10", 20, paso=15),
                "query": {"date": "2026-09-10", "status": "available"},
            },
        )
    )
    await runtime.execute("propose_slots", {"fecha": "2026-09-10"})
    assert len(await ctx.store.get_offered_slots(conv.id)) == min(20, MAX_OFFERED_DIA)


@pytest.mark.parametrize(
    ("status", "frase"),
    [
        ("closed", "no abre"),
        ("full", "ya no quedan horarios"),
        ("beyond_horizon", "todavía no se abre agenda"),
        ("past", "ya pasó"),
    ],
)
async def test_un_dia_sin_horas_dice_por_que(runtime_y_ctx, respx_mock, status, frase):
    runtime, _, _ = runtime_y_ctx
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(
            200,
            json={"slots": [], "query": {"date": "2026-09-17", "status": status, "horizonEnd": "2026-09-16"}},
        )
    )
    result = await runtime.execute("propose_slots", {"fecha": "2026-09-17"})
    assert result["ok"] is False
    assert result["estado"] == status
    assert frase in result["detalle"]


async def test_un_dia_sin_horas_no_borra_lo_ya_ofrecido(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.get(AVAILABILITY).mock(
        side_effect=[
            httpx.Response(200, json={"slots": _horas("2026-09-11", 3), "query": {"coveredUntil": "2026-09-15"}}),
            httpx.Response(200, json={"slots": [], "query": {"date": "2026-09-12", "status": "closed"}}),
        ]
    )
    await runtime.execute("propose_slots", {})
    await runtime.execute("propose_slots", {"fecha": "2026-09-12"})
    assert len(await ctx.store.get_offered_slots(conv.id)) == 3


async def test_si_el_crm_ignora_la_fecha_dice_lo_que_ve_sin_negar_la_tarde(
    runtime_y_ctx, respx_mock
):
    """El e2e contra raíz (escenario 3): «¿tienen espacio mañana en la tarde?»
    contra un CRM que ignora `date`. Veía solo las mañanas del reparto y
    contestó «mañana solo tengo por la mañana». Ese día NO se consultó."""
    runtime, ctx, conv = runtime_y_ctx
    reparto = _horas("2026-09-11", 2) + _horas("2026-09-10", 3)
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(200, json={"slots": reparto})
    )
    result = await runtime.execute("propose_slots", {"fecha": "2026-09-10"})
    assert result["ok"] is True
    assert result["consulta_por_dia"] == "no_disponible"
    # Lo que SÍ ve de ese día, primero; sin `dayIso`, el día sale de la zona
    # del negocio (15:00 UTC = 09:00 en CDMX, el mismo día).
    assert result["horas_que_veo_de_ese_dia"] == ["09:00", "09:30", "10:00"]
    assert [s["start_utc"][:10] for s in result["slots"]] == ["2026-09-10"] * 3 + ["2026-09-11"] * 2
    instrucciones = result["instrucciones"]
    assert "son solo ALGUNAS horas" in instrucciones
    assert "NUNCA digas que ese día solo hay mañana o tarde" in instrucciones
    # Lo que la corrida contra el raíz sin consulta por día todavía dejaba
    # pasar: «¿prefieres otro día para la tarde?» también niega esa tarde.
    assert "NO des a entender que ese día no la hay" in instrucciones
    assert "ofrécele que el equipo le confirme esa hora (handoff) o revisar otro día" in instrucciones
    # El espejo queda como la oferta que el CRM acaba de registrar: lo que se
    # le enseña al lead se puede reservar.
    assert len(await ctx.store.get_offered_slots(conv.id)) == 5


async def test_si_el_crm_ignora_la_fecha_y_ese_dia_no_viene_no_dice_que_no_hay(
    runtime_y_ctx, respx_mock
):
    runtime, _, _ = runtime_y_ctx
    reparto = [dict(h, dayIso="2026-09-10") for h in _horas("2026-09-10", 3)]
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(200, json={"slots": reparto})
    )
    result = await runtime.execute("propose_slots", {"fecha": "2026-09-17"})
    assert result["ok"] is True
    assert result["horas_que_veo_de_ese_dia"] == []
    assert "eso NO quiere decir que no haya" in result["instrucciones"]
    assert len(result["slots"]) == 3  # lo que sí hay, para ofrecer otro día


async def test_si_el_crm_ignora_la_fecha_y_no_da_nada_no_se_afirma_nada(
    runtime_y_ctx, respx_mock
):
    runtime, _, _ = runtime_y_ctx
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(200, json={"slots": []})
    )
    result = await runtime.execute("propose_slots", {"fecha": "2026-09-17"})
    assert result["ok"] is False
    assert result["error"] == "consulta_por_dia_no_disponible"
    assert "NO digas que ese día no hay agenda" in result["detalle"]
    assert "handoff" in result["detalle"]


async def test_fecha_mal_escrita_se_corrige_sin_ir_al_crm(runtime_y_ctx, respx_mock):
    runtime, _, _ = runtime_y_ctx
    route = respx_mock.get(AVAILABILITY).mock(return_value=httpx.Response(200, json={"slots": []}))
    result = await runtime.execute("propose_slots", {"fecha": "jueves"})
    assert result["error"] == "fecha_invalida"
    assert route.call_count == 0


async def test_con_fecha_la_lista_de_horas_va_aparte(runtime_y_ctx, respx_mock):
    # En la autoprueba con LLM real, con las 11:00 dentro de `slots`, el modelo
    # contestó "a las 11 no tengo espacio". La lista corta no se presta a eso.
    runtime, _, _ = runtime_y_ctx
    respx_mock.get(AVAILABILITY).mock(
        return_value=httpx.Response(
            200,
            json={"slots": _horas("2026-09-10", 8), "query": {"date": "2026-09-10", "status": "available"}},
        )
    )
    result = await runtime.execute("propose_slots", {"fecha": "2026-09-10"})
    assert "11:00" in result["horas_libres"]
    assert "SÍ está libre" in result["instrucciones"]


def test_el_prompt_trae_calendario_con_fechas_iso():
    # "La próxima semana, jueves o viernes" dicho el jueves 10 se volvía el
    # martes 15 y el miércoles 16. Con la tabla solo hay que buscar el renglón.
    from datetime import datetime, timezone

    from app.profile import BusinessProfile
    from app.prompt import build_system_prompt
    from app.state import Conversation

    ahora = datetime(2026, 9, 10, 13, 6, tzinfo=timezone.utc)  # jueves 07:06 CDMX
    prompt = build_system_prompt(
        profile=BusinessProfile(), context=None,
        conv=Conversation(id=1, wa_identity="x", greeted=True), now=ahora,
    )
    assert "hoy jueves 10 = 2026-09-10" in prompt
    assert "PRÓXIMA SEMANA: lunes 14 = 2026-09-14" in prompt and "jueves 17 = 2026-09-17" in prompt
    assert "viernes 18 = 2026-09-18" in prompt


def test_sin_agenda_no_hay_calendario():
    from datetime import datetime, timezone

    from app.profile import BusinessProfile
    from app.prompt import build_system_prompt
    from app.state import Conversation

    prompt = build_system_prompt(
        profile=BusinessProfile(), context=None,
        conv=Conversation(id=1, wa_identity="x", greeted=True),
        agenda=False, now=datetime(2026, 9, 10, 13, 6, tzinfo=timezone.utc),
    )
    assert "Calendario" not in prompt

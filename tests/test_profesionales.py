"""vocero-cloud 233 — un número, varios profesionales.

El CRM manda `booking.agendas` en el contexto y cada hueco trae `agenda`.
Nea pregunta con quién o para qué, pide los huecos de ese profesional y dice
con quién es cada horario. Con una sola agenda nada de esto aparece.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from app.crm_brains import BrainsCrmClient
from app.prompt import _lineas_profesionales
from app.tools import (
    ToolRuntime,
    _label_of,
    profesionales_del_contexto,
    resolver_profesional,
    tool_schemas,
)
from tests.conftest import CRM_CONV_ID, IDENTITY, make_ctx

PROS = [
    {"id": "agd_ana", "name": "Dra. Ana Pérez", "description": "Pediatría"},
    {"id": "agd_beto", "name": "Dr. Beto Gómez", "description": "Clínica general"},
]
CONTEXTO = {"booking": {"timezone": "America/Argentina/Buenos_Aires", "agendas": PROS}}


def test_profesionales_del_contexto():
    assert [p["id"] for p in profesionales_del_contexto(CONTEXTO)] == ["agd_ana", "agd_beto"]
    assert profesionales_del_contexto({"booking": {"timezone": "UTC"}}) == []
    assert profesionales_del_contexto(None) == []


@pytest.mark.parametrize(
    "valor,esperado",
    [
        ("agd_beto", "agd_beto"),
        ("Dra. Ana Pérez", "agd_ana"),
        ("ana perez", "agd_ana"),
        ("Beto", "agd_beto"),
        ("Dr.", None),  # ambiguo: los dos lo traen
        ("Carla", None),
        ("", None),
    ],
)
def test_resolver_profesional(valor, esperado):
    p = resolver_profesional(valor, profesionales_del_contexto(CONTEXTO))
    assert (p["id"] if p else None) == esperado


def test_la_etiqueta_dice_con_quien():
    start = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)
    raw = {"dayLabel": "mañana miércoles 18 de noviembre", "time": "10:00", "agenda": {"id": "agd_ana", "name": "Dra. Ana Pérez"}}
    assert _label_of(raw, start) == "mañana miércoles 18 de noviembre, 10:00 con Dra. Ana Pérez"
    sin = {"dayLabel": "mañana miércoles 18 de noviembre", "time": "10:00", "agenda": None}
    assert _label_of(sin, start) == "mañana miércoles 18 de noviembre, 10:00"


def test_el_prompt_pregunta_con_quien_solo_si_hay_varios():
    lineas = " ".join(_lineas_profesionales(CONTEXTO))
    assert "Dra. Ana Pérez (Pediatría)" in lineas and "Dr. Beto Gómez (Clínica general)" in lineas
    assert "averigua con quién o para qué" in lineas
    uno = " ".join(_lineas_profesionales({"booking": {"agendas": PROS[:1]}}))
    assert "Las citas son con Dra. Ana Pérez" in uno and "averigua" not in uno
    assert _lineas_profesionales({"booking": {"timezone": "UTC"}}) == []


def test_propose_slots_acepta_profesional():
    schema = next(t for t in tool_schemas() if t["function"]["name"] == "propose_slots")
    assert "profesional" in schema["function"]["parameters"]["properties"]


class _CrmQueAnota:
    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    async def consultar_huecos(self, conversation_id: str, **kw: Any) -> dict[str, Any]:
        self.llamadas.append(kw)
        return {
            "slots": [
                {
                    "startUtc": "2026-11-18T13:00:00.000Z",
                    "label": "mié 18 nov, 10:00 con Dr. Beto Gómez",
                    "dayLabel": "mañana miércoles 18 de noviembre",
                    "time": "10:00",
                    "agenda": {"id": "agd_beto", "name": "Dr. Beto Gómez"},
                }
            ],
            "query": None,
        }

    async def aclose(self) -> None:  # pragma: no cover
        return None


@pytest.fixture
async def runtime_y_crm():
    ctx = make_ctx()
    real = ctx.crm
    crm = _CrmQueAnota()
    ctx.crm = crm  # type: ignore[assignment]
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID, profesionales=profesionales_del_contexto(CONTEXTO))
    yield runtime, crm
    await real.aclose()


async def test_pide_los_huecos_del_profesional_por_su_nombre(runtime_y_crm):
    runtime, crm = runtime_y_crm
    result = await runtime.execute("propose_slots", {"profesional": "Beto"})
    assert crm.llamadas[-1]["agenda_id"] == "agd_beto"
    assert result["ok"] and result["slots"][0]["label"].endswith("con Dr. Beto Gómez")


async def test_sin_profesional_pide_a_todos(runtime_y_crm):
    runtime, crm = runtime_y_crm
    await runtime.execute("propose_slots", {})
    assert "agenda_id" not in crm.llamadas[-1]


async def test_un_profesional_que_no_existe_no_consulta(runtime_y_crm):
    runtime, crm = runtime_y_crm
    result = await runtime.execute("propose_slots", {"profesional": "Dra. Carla"})
    assert result["ok"] is False and result["error"] == "profesional_desconocido"
    assert {p["nombre"] for p in result["profesionales"]} == {"Dra. Ana Pérez", "Dr. Beto Gómez"}
    assert crm.llamadas == []


async def test_el_cliente_cloud_manda_agenda_id(respx_mock):
    client = BrainsCrmClient("https://crm.test", "synthetic", "org-a")
    ruta = respx_mock.get("https://crm.test/api/brains/agenda/slots").mock(
        return_value=httpx.Response(200, json={"slots": [], "query": None})
    )
    await client.consultar_huecos("cv_1", agenda_id="agd_beto")
    assert ruta.calls.last.request.url.params["agendaId"] == "agd_beto"
    await client.consultar_huecos("cv_1")
    assert "agendaId" not in ruta.calls.last.request.url.params
    await client.aclose()

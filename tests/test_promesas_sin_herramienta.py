"""Nea no ofrece lo que no puede hacer.

En el e2e contra Vocero raíz (escenario 4), al confirmar la hora de la cita:
«¿Te mando un recordatorio antes de la sesión?». El CRM raíz no manda
recordatorios y en modo estándar Nea no tiene con qué: el campo obligatorio
`recordatorios_aceptados` de book_session —que solo usa la agenda v2 de
Vocero Cloud— la empujaba a preguntar para poder llenarlo.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation
from app.tools import TOOL_SCHEMAS, tool_schemas
from tests.conftest import mock_crm_basics, wa_body

AHORA = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)
SIN_RECORDATORIOS = "Este negocio NO manda recordatorios por aquí"


def _book(schemas):
    return next(t for t in schemas if t["function"]["name"] == "book_session")["function"]["parameters"]


def _prompt(**kw) -> str:
    return build_system_prompt(
        profile=BusinessProfile(), context=None, conv=Conversation(id=1, wa_identity="x"), now=AHORA, **kw
    )


def test_en_modo_estandar_book_session_no_pide_recordatorios():
    params = _book(tool_schemas(agenda_enabled=True))
    assert "recordatorios_aceptados" not in params["properties"]
    assert "recordatorios_aceptados" not in params["required"]
    assert params["required"] == ["start_utc", "dia_confirmado"]


def test_con_la_agenda_v2_el_consentimiento_sigue_siendo_obligatorio():
    params = _book(tool_schemas(agenda_enabled=True, agenda_v2=True))
    assert params["properties"]["recordatorios_aceptados"]["type"] == "boolean"
    assert "recordatorios_aceptados" in params["required"]
    # La v2 trabaja sobre una copia: el catálogo estándar no se contamina.
    assert "recordatorios_aceptados" not in _book(TOOL_SCHEMAS)["properties"]


def test_el_prompt_dice_que_aqui_no_hay_recordatorios_solo_sin_la_capacidad():
    assert SIN_RECORDATORIOS in _prompt(agenda=True)
    assert SIN_RECORDATORIOS not in _prompt(agenda=True, recordatorios=True)
    assert SIN_RECORDATORIOS not in _prompt(agenda=False)  # sin agenda no hay citas


def test_los_nunca_siguen_todos_y_se_suma_el_de_no_prometer_sin_herramienta():
    prompt = _prompt(agenda=True)
    nunca = prompt[prompt.index("NUNCA:"):prompt.index("MULTIMEDIA")]
    for regla in (
        "Inventes datos, precios, casos o features.",
        "Inventes fallas del sistema ni motivos que no te dio el contexto",
        "Prometas resultados que el negocio no aprobó por escrito.",
        "Uses jerga técnica",
        "Digas qué modelo, proveedor o versión de IA te ejecuta",
        "Ruegues la cita ni hagas hard-sell.",
        "Sigas vendiendo a quien te insulta.",
        "Pidas datos sensibles (pagos, contraseñas).",
        "Te salgas del tema",
    ):
        assert regla in nunca, regla
    assert "Ofrezcas ni prometas una acción que no puedes hacer con tus herramientas" in nunca
    assert "Nunca finges ser humano" in prompt


async def test_el_turno_estandar_no_le_ensena_recordatorios_al_modelo(ctx, client, respx_mock):
    mock_crm_basics(respx_mock)
    await client.post("/webhook", content=wa_body(text="quiero agendar"))
    await asyncio.sleep(0.25)
    llamada = ctx.llm.calls[0]
    assert "recordatorios_aceptados" not in _book(llamada["tools"])["properties"]
    assert SIN_RECORDATORIOS in llamada["messages"][0]["content"]

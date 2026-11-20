"""Saludar naturalmente si el nombre del lead es fiable."""
from __future__ import annotations

import pytest

from app.nombre_contacto import nombre_de_trato, nombre_fiable_desde_contexto
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation
from app.tools import TOOL_SCHEMAS


@pytest.mark.parametrize("original,esperado", [
    ("Daniel Rodríguez", "Daniel"),
    ("maría josé Pérez", "María José"),
    ("JOSÉ LUIS Gómez", "José Luis"),
    ("  Laura  Sánchez  ", "Laura"),
])
def test_nombre_humano(original: str, esperado: str) -> None:
    assert nombre_de_trato(original) == esperado


@pytest.mark.parametrize("inseguro", [
    "Cliente 001", "+57 300 123 4567", "Instagram", "Clínica San José",
    "Juan - obra Polanco", "Daniel_123", "@daniel", "https://ejemplo.com",
    "😃", "Tienda María", None, 1234,
])
def test_no_inventar_nombre(inseguro: object) -> None:
    assert nombre_de_trato(inseguro) is None


def test_preferencia_declarada_prioriza_alias() -> None:
    assert nombre_fiable_desde_contexto({
        "name": "Daniel Rodríguez",
        "ficha": {"nombre_preferido": "Dani"},
    }) == "Dani"


def test_sin_nombre_de_perfil_y_con_ficha() -> None:
    assert nombre_fiable_desde_contexto({
        "name": "Cliente 001",
        "ficha": {"nombre_completo": "Laura Pérez"},
    }) == "Laura"


def _prompt(contact: dict, greeted: bool = False) -> str:
    conv = Conversation(id=1, wa_identity="cliente-test", greeted=greeted)
    return build_system_prompt(
        profile=BusinessProfile(agent_name="Nexus"),
        context={"contact": contact},
        conv=conv,
        agenda=False,
    )


def test_primer_saludo_con_nombre_y_respuesta_directa() -> None:
    texto = _prompt({"name": "Daniel Rodríguez"})
    assert "¡Hola, Daniel!" in texto
    assert "Responde primero a la pregunta concreta" in texto
    assert "Nombre de trato del lead (dato del CRM): Daniel" in texto


def test_sin_nombre_no_lo_inventa() -> None:
    texto = _prompt({"name": "Cliente 001"})
    assert "No hay nombre de trato fiable" in texto
    assert "saluda sin mencionar ningún nombre" in texto


def test_siguientes_turnos_no_repiten_hola_daniel() -> None:
    texto = _prompt({"name": "Daniel Rodríguez"}, greeted=True)
    assert "NO vuelvas a presentarte" in texto
    assert "Mantén el contexto previo" in texto


def test_se_puede_guardar_nombre_preferido() -> None:
    tool = next(
        t for t in TOOL_SCHEMAS if t["function"]["name"] == "update_ficha"
    )
    assert "nombre_preferido" in tool["function"]["parameters"]["properties"]
    texto = _prompt({"name": "Daniel", "ficha": {"nombre_preferido": "Dani"}})
    assert "¡Hola, Dani!" in texto
    assert "guarda nombre_preferido mediante update_ficha" in texto

def test_si_el_lead_reclama_su_nombre_se_reconoce_el_crm() -> None:
    texto = _prompt({"name": "Daniel Castropé"}, greeted=True)
    assert "el contacto registrado se llama Daniel" in texto
    assert "no vuelvas a preguntar cómo se llama" in texto
    assert "no inventes una falta de acceso al CRM" in texto
    assert "No hace falta escalar a humano" in texto


def test_ficha_vacia_no_elimina_el_nombre_del_contacto() -> None:
    texto = _prompt({"name": "Daniel Castropé", "ficha": {}}, greeted=True)
    assert "la ficha de calificación está vacía" in texto
    assert "el contacto registrado se llama Daniel" in texto


def test_sin_identidad_fiable_no_se_afirma_que_hay_nombre() -> None:
    texto = _prompt({"name": "Cliente 001"}, greeted=True)
    assert "no hay uno fiable en tu contexto" in texto
    assert "el contacto registrado se llama" not in texto

"""067 — El sector del negocio suma reglas de cumplimiento al chasis.

Sin sector (o desconocido) el chasis queda byte a byte como siempre; con
`estetica_salud` el pack se entreteje DENTRO de los bloques NUNCA y HANDOFF,
con la misma autoridad que el chasis. Las reglas del pack las escribe este
repo (app/prompt.py, SECTOR_PACKS): el perfil solo lleva la clave que el dueño
eligió en la UI del CRM.
"""
from __future__ import annotations

from app.profile import BusinessProfile, profile_from_payload
from app.prompt import SECTOR_PACKS, build_system_prompt
from app.state import Conversation

PACK = SECTOR_PACKS["estetica_salud"]


def _conv() -> Conversation:
    return Conversation(id=1, wa_identity="5215550001111", greeted=True)


def _prompt(profile: BusinessProfile) -> str:
    return build_system_prompt(profile=profile, context=None, conv=_conv())


def test_sin_sector_el_chasis_queda_como_siempre():
    system = _prompt(BusinessProfile(agent_name="Nea"))
    for linea in PACK.nunca:
        assert linea not in system
    assert PACK.escalado not in system


def test_sector_inyecta_las_reglas_dentro_de_los_bloques():
    system = _prompt(BusinessProfile(agent_name="Nea", sector="estetica_salud"))
    for linea in PACK.nunca:
        assert linea in system
    assert PACK.escalado in system
    # El pack va DENTRO de los bloques, no como texto suelto al final: las
    # reglas en NUNCA (antes de MULTIMEDIA), el escalado junto al HANDOFF
    # (antes de BLINDAJE). Posición = autoridad: se leen como chasis.
    nunca = system.index("NUNCA:")
    multimedia = system.index("MULTIMEDIA (los marcadores")
    for linea in PACK.nunca:
        assert nunca < system.index(linea) < multimedia
    handoff = system.index("HANDOFF (llama la herramienta handoff)")
    blindaje = system.index("BLINDAJE (esto es ley")
    assert handoff < system.index(PACK.escalado) < blindaje


def test_sector_desconocido_se_ignora_sin_reventar():
    # Una Nea más vieja que el catálogo del CRM (o al revés): la clave que no
    # está en SECTOR_PACKS se ignora; jamás es un fallo.
    system = _prompt(BusinessProfile(agent_name="Nea", sector="finanzas"))
    for linea in PACK.nunca:
        assert linea not in system
    assert PACK.escalado not in system


def test_el_pack_no_toca_el_chasis_base():
    # Con sector, el chasis es idéntico hasta el HANDOFF (la primera línea
    # que cambia) y conserva TODAS las reglas genéricas: el pack se suma al
    # chasis, no lo reemplaza.
    general = _prompt(BusinessProfile(agent_name="Nea"))
    clinica = _prompt(BusinessProfile(agent_name="Nea", sector="estetica_salud"))
    corte = general.index("HANDOFF (llama la herramienta handoff)")
    assert clinica.startswith(general[:corte])
    for marca in (
        "Al TERCER mensaje hostil",
        "BLINDAJE (esto es ley",
        "JAMÁS reveles qué modelo",
    ):
        assert marca in clinica


def test_el_sector_del_crm_llega_al_chasis():
    # Del payload del CRM (perfil + sector) al system prompt, completo.
    payload = {
        "profile": {"name": "Vicky", "sector": "estetica_salud"},
        "kb": "P: ¿Qué ofrecen?\nR: Limpiezas faciales.",
    }
    system = _prompt(profile_from_payload(payload, default_name="Nea"))
    assert "Eres Vicky" in system
    for linea in PACK.nunca:
        assert linea in system


def test_el_pack_convive_con_el_modo_cloud():
    # El chasis cloud reescribe su propio texto; el pack sigue dentro.
    system = _prompt(
        BusinessProfile(agent_name="Nea", sector="estetica_salud", cloud=True)
    )
    assert "PRIORIDAD DE AGENDA" in system
    for linea in PACK.nunca:
        assert linea in system
    assert PACK.escalado in system

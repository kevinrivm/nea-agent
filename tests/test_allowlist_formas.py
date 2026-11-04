"""ALLOWED_WA_IDS / TESTER_WA_IDS aceptan el teléfono como se escriba.

El CRM manda la identidad solo con dígitos (521… → 52…). Con el «+» del
teléfono en la lista, la allowlist no coincidía nunca y Nea no le contestaba
ni al dueño en modo prueba (ensayo del starter 3.0.0, 2026-10-21).
"""
from __future__ import annotations

import pytest

from app.config import Settings

DUENO = "524621349768"


@pytest.mark.parametrize(
    "escrito",
    [
        "524621349768",
        "5214621349768",
        "+524621349768",
        "+5214621349768",
        "+52 1 462 134 9768",
        "52-462-134-9768",
        "(52) 462 134 9768",
    ],
)
def test_el_telefono_escrito_de_cualquier_forma_coincide_con_la_identidad_del_crm(escrito: str) -> None:
    s = Settings(allowed_wa_ids=escrito, tester_wa_ids=escrito)
    assert s.allowed_identities == frozenset({DUENO})
    assert s.tester_identities == frozenset({DUENO})


def test_varias_entradas_y_las_que_no_son_telefono_no_cambian() -> None:
    s = Settings(allowed_wa_ids="+52 462 134 9768, bsuid:MX.abc123, US.987, ig:555, tt:abc")
    assert s.allowed_identities == frozenset(
        {DUENO, "bsuid:MX.abc123", "bsuid:US.987", "ig:555", "tt:abc"}
    )


def test_vacia_sigue_siendo_sin_restriccion() -> None:
    assert Settings(allowed_wa_ids="").allowed_identities == frozenset()

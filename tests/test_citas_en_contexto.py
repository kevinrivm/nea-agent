"""Las citas del lead llegan del CRM y mandan sobre el historial.

Fija los tres fallos que dio Tobaxis en producción (14 sep 2026) cuando el
agente solo sabía de las citas por lo que recordaba del chat:
- reservó una segunda cita para quien no llegó a la suya;
- a las 17:04 le dijo a un cliente que su demo era "hoy a las 10:30";
- ante "pásame la liga" escaló a un humano teniendo el enlace.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.crm import AgendaUnavailable, CrmConflict
from app.crm_brains import BrainsCrmClient
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation

CRM_URL = "http://crm.test"
LINK = "https://meet.google.com/aza-iftw-wxp"

# Lunes 14 sep 2026, cita 10:30–11:00 en CDMX.
CITA = {
    "id": "bk_1",
    "startUtc": "2026-09-14T16:30:00.000Z",
    "endUtc": "2026-09-14T17:00:00.000Z",
    "label": "hoy lunes, 14 de septiembre, 10:30",
    "meetingLink": LINK,
    "linkPending": False,
}


def _prompt(booking: object, now: datetime) -> str:
    context = {"contact": {"name": "Gerardo"}}
    if booking is not None:
        context["booking"] = booking
    return build_system_prompt(
        profile=BusinessProfile(),
        context=context,
        conv=Conversation(id=1, wa_identity="525551851055", greeted=True),
        now=now,
    )


def _bloque(booking: object, now: datetime) -> str:
    return _prompt(booking, now).split("CONTEXTO ACTUAL:", 1)[1]


def test_la_cita_que_viene_trae_su_enlace_para_no_escalar():
    antes = datetime(2026, 9, 14, 15, 59, tzinfo=timezone.utc)  # 09:59 CDMX
    bloque = _bloque(
        {"timezone": "America/Mexico_City", "next": CITA, "unresolved": None}, antes
    )
    assert "YA tiene cita agendada: hoy lunes, 14 de septiembre, 10:30" in bloque
    assert LINK in bloque
    assert "NO es handoff" in bloque
    assert "reschedule_session" in bloque


def test_la_cita_que_ya_paso_no_se_da_por_vigente():
    tarde = datetime(2026, 9, 14, 23, 4, tzinfo=timezone.utc)  # 17:04 CDMX
    bloque = _bloque(
        {"timezone": "America/Mexico_City", "next": None, "unresolved": CITA}, tarde
    )
    assert "YA PASÓ" in bloque
    assert "book_session" in bloque
    assert "YA tiene cita agendada" not in bloque
    # Una cita que ya pasó no invita a mandar el enlace como si sirviera.
    assert LINK not in bloque


def test_la_cita_en_curso_si_lleva_el_enlace():
    durante = datetime(2026, 9, 14, 16, 40, tzinfo=timezone.utc)  # 10:40 CDMX
    bloque = _bloque(
        {"timezone": "America/Mexico_City", "next": None, "unresolved": CITA}, durante
    )
    assert "EN CURSO" in bloque
    assert "termina a las 11:00" in bloque
    assert LINK in bloque


def test_sin_citas_el_historial_no_puede_revivir_una():
    ahora = datetime(2026, 9, 14, 23, 4, tzinfo=timezone.utc)
    bloque = _bloque(
        {"timezone": "America/Mexico_City", "next": None, "unresolved": None}, ahora
    )
    assert "NO tiene ninguna cita por delante" in bloque


def test_enlace_pendiente_no_se_promete():
    antes = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
    cita = {**CITA, "meetingLink": None, "linkPending": True}
    bloque = _bloque({"next": cita, "unresolved": None}, antes)
    assert "le llega por aquí" in bloque


def test_crm_sin_bloque_de_citas_no_afirma_nada():
    # CRM viejo o agenda apagada: callar es mejor que decir "no tiene cita".
    ahora = datetime(2026, 9, 14, 23, 4, tzinfo=timezone.utc)
    bloque = _bloque(None, ahora)
    assert "cita" not in bloque.lower()


def test_el_chasis_dice_que_el_contexto_manda_sobre_el_historial():
    ahora = datetime(2026, 9, 14, 23, 4, tzinfo=timezone.utc)
    assert "no las que recuerdes del historial" in _prompt(None, ahora)


# ── El 404 ambiguo al mover una cita en modo cloud ────────────────────────


@pytest.fixture
def cliente() -> BrainsCrmClient:
    return BrainsCrmClient(CRM_URL, "secreto", "mi-negocio")


@respx.mock
async def test_mover_sin_cita_por_delante_no_apaga_la_agenda(cliente):
    # El CRM responde 404 CON cuerpo cuando no hay cita que mover. Leerlo como
    # "agenda apagada" desactivaba el agendamiento de la instancia entera.
    respx.patch(f"{CRM_URL}/api/brains/agenda/book").mock(
        return_value=httpx.Response(
            404,
            json={"ok": False, "code": "not_found", "message": "No hay una cita activa que mover", "slots": []},
        )
    )
    with pytest.raises(CrmConflict) as exc:
        await cliente.reschedule_booking("cv_1", "2026-09-15T15:00:00Z")
    assert exc.value.code == "no_booking"


@respx.mock
async def test_mover_con_la_agenda_apagada_sigue_siendo_agenda_apagada(cliente):
    respx.patch(f"{CRM_URL}/api/brains/agenda/book").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(AgendaUnavailable):
        await cliente.reschedule_booking("cv_1", "2026-09-15T15:00:00Z")

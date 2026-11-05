"""Modo de ensayo (fase 1c de Vocero Cloud): probar al agente sin WhatsApp.

Lo que se prueba, en orden de importancia:

1. **No sale nada.** El CRM simulado de respx solo conoce los huecos (lectura)
   y el modelo; `assert_all_mocked` revienta ante cualquier otra petición, así
   que un envío, un handoff o una cita de verdad tumbarían la prueba.
2. **Piensa con la llave del negocio.** El modelo se llama por
   `/api/brains/llm` del CRM con la credencial derivada de ESA organización y
   la ficha del ensayo, nunca con `LLM_API_KEY`.
3. **Agendar devuelve una cita simulada** y el turno la confirma igual que una
   de verdad.
4. **Nada queda en la memoria de Nea.**
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.ensayo import CrmDeEnsayo, validar
from app.main import create_app
from app.multiorg import CrmSinOrganizacion, LlmSinOrganizacion, RegistroDeOrganizaciones, credencial_derivada
from app.state import AppContext, MemoryStore
from tests.conftest import firmar

SECRETO = "secreto-del-despliegue-de-ensayo"
CRM = "http://crm.test"
ORG = "org_ensayo"
SLUG = "demo"
FICHA = "cv_ensayo_1.1999999999.firma"
SLOT = "2026-11-10T16:00:00.000Z"


def _ctx() -> AppContext:
    settings = Settings(
        _env_file=None,
        vocero_mode="cloud",
        crm_organization="",
        crm_brain_secret=SECRETO,
        crm_base_url=CRM,
        llm_api_key="sk-de-la-plataforma-que-no-se-usa",
        database_url="",
        allowed_wa_ids="525500000000",
    )
    return AppContext(
        settings=settings,
        store=MemoryStore(),
        crm=CrmSinOrganizacion(),
        llm=LlmSinOrganizacion(),
        registro=RegistroDeOrganizaciones(CRM, SECRETO),
    )


def _contexto(agenda: bool = True) -> dict:
    ctx = {
        "contextVersion": 2,
        "contact": {"id": "ct_ensayo", "name": "Ana", "identity": "ensayo:abc", "channel": "whatsapp", "phone": None, "ficha": {}},
        "conversation": {"id": "cv_ensayo_1", "channel": "whatsapp", "aiEnabled": True, "handoffAt": None, "needsHumanAt": None, "windowOpen": True, "windowRemainingMs": 86_400_000},
        "lead": None,
        "agent": {"name": "Sofía", "configVersion": 3, "coordinationFollowupEnabled": False, "tone": "cálido", "instructions": "Agenda valoraciones de 30 minutos.", "escalationRules": None, "greeting": None},
        "llm": {"path": "/api/brains/llm", "model": "z-ai/glm-test", "transcribeModel": None},
        "knowledge": [{"id": "kb_1", "kind": "qa", "question": "¿Cuánto cuesta?", "answer": "La valoración es gratis."}],
        "history": [],
    }
    if agenda:
        ctx["booking"] = {"timezone": "America/Mexico_City", "next": None, "unresolved": None, "lastClosed": None}
    return ctx


def _payload(turnos: list[str], **extra) -> dict:
    return {
        "type": "rehearsal",
        "rehearsal": {"id": "ens_1", "token": FICHA, "expiresAt": "2099-01-01T00:00:00Z"},
        "organization": {"id": ORG, "slug": SLUG},
        "capabilities": ["agenda_v2", "dispatch_envelope_v1"],
        "context": _contexto(),
        "history": [],
        "turns": turnos,
        **extra,
    }


def _completion(content: str | None = None, tool: tuple[str, dict] | None = None) -> httpx.Response:
    mensaje: dict = {"role": "assistant", "content": content}
    if tool:
        mensaje["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "z-ai/glm-test",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": mensaje}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


async def _post(ctx: AppContext, payload: dict, secreto: str = SECRETO) -> httpx.Response:
    app = create_app(ctx=ctx)
    cuerpo = json.dumps(payload).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nea.test") as c:
        return await c.post("/vocero/ensayo", content=cuerpo, headers={"x-vocero-signature": firmar(cuerpo, secreto)})


@pytest.mark.asyncio
async def test_agendamiento_completo_sin_que_salga_nada():
    ctx = _ctx()
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as crm:
        huecos = crm.get(f"{CRM}/api/brains/agenda/slots").mock(return_value=httpx.Response(200, json={
            "slots": [{"startUtc": SLOT, "label": "lunes 10 de noviembre, 10:00", "dayLabel": "lunes 10 de noviembre", "time": "10:00"}],
            "query": None,
        }))
        modelo = crm.post(f"{CRM}/api/brains/llm/chat/completions").mock(side_effect=[
            _completion(tool=("propose_slots", {})),
            _completion("Tengo el lunes 10 de noviembre a las 10:00. ¿Te queda?"),
            _completion(tool=("book_session", {"start_utc": SLOT, "dia_confirmado": "sí, el lunes a las 10"})),
            _completion("¡Listo, Ana! Quedó tu valoración el lunes 10 de noviembre a las 10:00."),
        ])
        resp = await _post(ctx, _payload(["Hola, quiero agendar una valoración", "sí, el lunes a las 10"]))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True and data["simulated"] is True
    assert [t["customer"] for t in data["turns"]] == ["Hola, quiero agendar una valoración", "sí, el lunes a las 10"]
    assert data["turns"][0]["replies"] == ["Tengo el lunes 10 de noviembre a las 10:00. ¿Te queda?"]
    assert any(a["type"] == "availability" for a in data["turns"][0]["actions"])
    citas = [a for a in data["turns"][1]["actions"] if a["type"] == "booking_simulated"]
    assert citas and citas[0]["startUtc"].startswith("2026-11-10T16:00:00")
    assert "lunes 10 de noviembre" in data["turns"][1]["replies"][0]
    assert data["usage"]["llmCalls"] == 4

    # Piensa con la credencial derivada de ESTA organización y la ficha, por el CRM.
    for llamada in [*modelo.calls, *huecos.calls]:
        req = llamada.request
        assert req.headers["authorization"] == f"Bearer {credencial_derivada(SECRETO, ORG)}"
        assert req.headers["x-vocero-organization"] == SLUG
        assert req.headers["x-vocero-ensayo"] == FICHA
    assert huecos.calls[0].request.url.params["conversationId"] == "cv_ensayo_1"
    # La llave de la plataforma no viaja nunca.
    assert all("sk-de-la-plataforma" not in str(c.request.headers) for c in modelo.calls)

    # Nada quedó en la memoria de Nea.
    assert ctx.store.conversations == {}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pedir_una_persona_hace_handoff_y_despues_calla():
    ctx = _ctx()
    with respx.mock(assert_all_mocked=True) as crm:
        crm.post(f"{CRM}/api/brains/llm/chat/completions").mock(side_effect=[
            _completion(tool=("handoff", {"reason": "cliente"})),
            _completion("Claro, te paso con alguien del equipo."),
        ])
        resp = await _post(ctx, _payload(["Quiero hablar con una persona", "¿hola?"]))
    data = resp.json()
    assert data["turns"][0]["replies"] == ["Claro, te paso con alguien del equipo."]
    assert {"type": "handoff", "reason": "cliente"} in data["turns"][0]["actions"]
    # Tras el handoff la IA se calla, como en el CRM de verdad.
    assert data["turns"][1]["replies"] == [] and data["turns"][1]["actions"] == []


@pytest.mark.asyncio
async def test_sin_ia_del_negocio_no_se_piensa():
    ctx = _ctx()
    payload = _payload(["hola"])
    payload["context"]["llm"] = None
    with respx.mock(assert_all_mocked=True):
        resp = await _post(ctx, payload)
    assert resp.json() == {"ok": False, "error": "sin_credencial", "turns": []}


@pytest.mark.asyncio
async def test_firma_de_otro_secreto_es_401():
    with respx.mock(assert_all_mocked=True):
        resp = await _post(_ctx(), _payload(["hola"]), secreto="otro")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_fuera_de_multiorg_no_existe():
    ctx = _ctx()
    ctx.registro = None
    resp = await _post(ctx, _payload(["hola"]))
    assert resp.status_code == 404


def test_validar_rechaza_lo_que_no_es_un_ensayo():
    assert validar(_payload(["hola"])) is None
    assert validar({**_payload(["hola"]), "type": "dispatch"}) == "tipo"
    assert validar(_payload([])) == "turnos"
    assert validar(_payload(["x"] * 13)) == "turnos"
    assert validar(_payload(["hola"], history=[{"role": "system", "text": "x"}])) == "historial"
    sin_ficha = _payload(["hola"])
    sin_ficha["rehearsal"]["token"] = ""
    assert validar(sin_ficha) == "ficha"


@pytest.mark.asyncio
async def test_el_crm_de_ensayo_no_sabe_escribir_lo_que_no_conoce():
    crm = CrmDeEnsayo(_contexto(), object(), [])  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await crm.post_lo_que_sea("cv")
    await crm.send_message("cv", "hola")
    assert crm.eventos == [{"type": "message", "text": "hola"}]

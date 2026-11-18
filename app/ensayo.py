"""Modo de ensayo: el dueño prueba a su agente sin WhatsApp (fase 1c).

El CRM manda una conversación de mentira —lo que «el cliente» escribe, turno
por turno— y Nea contesta **con el mismo turno de siempre** (`run_turn`): el
mismo prompt, las mismas herramientas, la configuración real del negocio y su
llave de OpenRouter por `/api/brains/llm`. Si el ensayo usara un agente
distinto del que contesta de verdad, la prueba engañaría al dueño.

Lo que NO hace, y es la razón de que exista este archivo:

- **No manda nada.** El cliente del CRM del ensayo (`CrmDeEnsayo`) no tiene
  cómo escribir: lo que el turno «envía» se guarda en una lista y vuelve en la
  respuesta. Igual el handoff, la ficha, la etapa y las citas.
- **No escribe en la bandeja ni en la memoria de Nea.** El turno corre sobre
  un `MemoryStore` nuevo que muere con la petición: ni el historial, ni los
  seguimientos, ni los envíos pendientes de un ensayo llegan a la base.
- **No crea citas.** Consulta los huecos de verdad (solo lectura: con la ficha
  del ensayo el CRM no los registra como oferta) y agendar devuelve una cita
  simulada que solo existe en esta respuesta.

La entrada va firmada con el secreto del despliegue, igual que un despacho. La
ficha (`ensayo.token`) la firma el CRM para UNA organización y UNA
conversación inventada, caduca en minutos, y Nea solo la reenvía: con ella el
CRM deja pensar y consultar huecos sin tocar nada.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.crm import CrmConflict
from app.crm_brains import BrainsCrmClient, _perfil_desde_contexto
from app.dispatch import firma_valida
from app.http_limits import limited_body
from app.multiorg import organizacion_del_despacho
from app.profile import ProfileProvider
from app.state import AppContext, InboundMessage, MemoryStore
from app.turn import handle_flush

logger = logging.getLogger("nea.ensayo")

router = APIRouter()

TIPO = "rehearsal"
MAX_TURNOS = 12
MAX_HISTORIAL = 40
MAX_TEXTO = 2000


class CrmDeEnsayo:
    """El CRM de un ensayo: lee de verdad lo que es de solo lectura y NADA más.

    No hereda de `CrmClient` a propósito. Si heredara, un método nuevo del
    turno que escriba en el CRM funcionaría aquí sin que nadie lo notara —y el
    ensayo le mandaría un WhatsApp a nadie, o una cita al calendario del
    negocio—. Así, un método que no está aquí revienta (`__getattr__`), el
    turno cae a su red de seguridad y el ensayo lo cuenta como error.
    """

    def __init__(
        self,
        contexto: dict[str, Any],
        lectura: BrainsCrmClient,
        capacidades: list[str],
    ) -> None:
        self._contexto = contexto
        self._lectura = lectura
        self.eventos: list[dict[str, Any]] = []
        self.citas: list[dict[str, Any]] = []
        self._perfil = _perfil_desde_contexto(contexto)
        self.supports_agenda_v2 = "agenda_v2" in capacidades
        self.supports_coordination = bool(
            (contexto.get("agent") or {}).get("coordinationFollowupEnabled")
        )
        self._n = 0

    # ---------------------------------------------------------- lectura ---

    async def get_context(self, wa_identity: str) -> dict[str, Any] | None:
        return self._contexto

    async def get_profile(self) -> dict[str, Any] | None:
        return self._perfil

    async def agenda_available(self) -> bool:
        return self._contexto.get("booking") is not None

    async def sondear_agenda(self, timeout: float | None = None) -> bool | None:
        return self._contexto.get("booking") is not None

    async def get_availability(self, conversation_id: str, **kw: Any) -> list[dict[str, Any]]:
        slots = await self._lectura.get_availability(conversation_id, **kw)
        self.eventos.append({"type": "availability", "slots": len(slots)})
        return slots

    async def consultar_huecos(self, conversation_id: str, date: str | None = None, **kw: Any) -> dict[str, Any]:
        consulta = await self._lectura.consultar_huecos(conversation_id, date=date, **kw)
        self.eventos.append(
            {"type": "availability", "date": date, "slots": len(consulta.get("slots") or [])}
        )
        return consulta

    async def list_bookings(self, conversation_id: str) -> list[dict[str, Any]]:
        return [c for c in self.citas if c["status"] == "agendada"]

    # -------------------------------------------- escritura (simulada) ---

    async def send_message(self, conversation_id: str, text: str, dispatch_id: str = "") -> dict[str, Any]:
        self._n += 1
        self.eventos.append({"type": "message", "text": text})
        return {"ok": True, "messageId": f"msg_ensayo_{self._n}", "simulated": True}

    async def post_typing(self, conversation_id: str) -> None:
        return None

    async def post_handoff(self, conversation_id: str, reason: str | None = None) -> None:
        self.eventos.append({"type": "handoff", "reason": reason})
        # Lo que hace el CRM de verdad: con handoff la IA se calla. Así el
        # ensayo enseña que tras «quiero hablar con una persona» el agente ya
        # no contesta, que es lo que va a ver el cliente.
        conv = self._contexto.setdefault("conversation", {})
        conv["aiEnabled"] = False
        conv["handoffAt"] = "ensayo"

    async def put_ficha(self, conversation_id: str, ficha: dict[str, Any]) -> dict[str, Any]:
        self.eventos.append({"type": "ficha", "fields": ficha})
        contacto = self._contexto.setdefault("contact", {})
        contacto["ficha"] = {**(contacto.get("ficha") or {}), **ficha}
        return {"ficha": contacto["ficha"], "stageMoved": False, "simulated": True}

    async def move_stage(self, conversation_id: str, stage: str) -> dict[str, Any]:
        self.eventos.append({"type": "stage", "stage": stage})
        return {"ok": True, "lead": {"stageName": stage}, "simulated": True}

    async def set_coordination_consent(self, conversation_id: str, consent: bool) -> dict[str, Any]:
        self.eventos.append({"type": "coordination_consent", "consent": consent})
        return {"ok": True, "simulated": True}

    async def create_booking(self, conversation_id: str, start_utc: str, reminder_consent: bool = False) -> dict[str, Any]:
        cita = {
            "id": f"bk_ensayo_{len(self.citas) + 1}",
            "startUtc": start_utc,
            "status": "agendada",
            "reminderConsent": reminder_consent,
            "selectionToken": f"sel_ensayo_{len(self.citas) + 1}",
        }
        self.citas.append(cita)
        self.eventos.append({"type": "booking_simulated", "startUtc": start_utc, "reminderConsent": reminder_consent})
        self._cita_en_contexto(cita)
        # Sin enlace de reunión: inventarlo sería peor que no tenerlo (el
        # dueño lo vería y creería que su Zoom ya funciona). El turno confirma
        # día y hora, que es lo que el ensayo tiene que enseñar.
        return {"ok": True, **cita, "meetingLink": None, "linkPending": False, "simulated": True}

    async def reschedule_booking(self, conversation_id: str, start_utc: str, selection_token: str | None = None) -> dict[str, Any]:
        vigentes = [c for c in self.citas if c["status"] == "agendada"]
        if not vigentes:
            raise CrmConflict("no_booking", {})
        cita = next((c for c in vigentes if c["selectionToken"] == selection_token), vigentes[-1])
        cita["startUtc"] = start_utc
        self.eventos.append({"type": "booking_rescheduled_simulated", "startUtc": start_utc})
        self._cita_en_contexto(cita)
        return {"ok": True, **cita, "meetingLink": None, "linkPending": False, "simulated": True}

    async def cancel_booking(self, conversation_id: str, selection_token: str, confirmation: bool) -> dict[str, Any]:
        cita = next((c for c in self.citas if c["selectionToken"] == selection_token), None)
        if cita is None:
            raise CrmConflict("no_booking", {})
        cita["status"] = "cancelada"
        self.eventos.append({"type": "booking_cancelled_simulated", "startUtc": cita["startUtc"]})
        booking = self._contexto.get("booking")
        if isinstance(booking, dict):
            booking["next"] = None
        return {"ok": True, "simulated": True}

    async def post_reset(self, conversation_id: str) -> None:
        return None

    async def aclose(self) -> None:
        await self._lectura.aclose()

    def _cita_en_contexto(self, cita: dict[str, Any]) -> None:
        booking = self._contexto.get("booking")
        if isinstance(booking, dict):
            booking["next"] = {
                "id": cita["id"],
                "startUtc": cita["startUtc"],
                "label": cita["startUtc"],
                "meetingLink": None,
                "linkPending": False,
            }

    def __getattr__(self, nombre: str) -> Any:
        raise RuntimeError(
            f"el ensayo no sabe hacer {nombre}: un ensayo no escribe en el CRM"
        )


def _texto(valor: Any) -> str | None:
    if not isinstance(valor, str):
        return None
    valor = valor.strip()
    return valor[:MAX_TEXTO] if valor else None


def validar(payload: Any) -> str | None:
    """Por qué este ensayo no se puede correr, o None si se puede."""
    if not isinstance(payload, dict) or payload.get("type") != TIPO:
        return "tipo"
    if organizacion_del_despacho(payload) is None:
        return "organizacion"
    ensayo = payload.get("rehearsal")
    if not isinstance(ensayo, dict) or not isinstance(ensayo.get("token"), str) or not ensayo["token"]:
        return "ficha"
    contexto = payload.get("context")
    if not isinstance(contexto, dict) or not isinstance((contexto.get("conversation") or {}).get("id"), str):
        return "contexto"
    if not isinstance((contexto.get("contact") or {}).get("identity"), str):
        return "contexto"
    turnos = payload.get("turns")
    if not isinstance(turnos, list) or not 0 < len(turnos) <= MAX_TURNOS:
        return "turnos"
    if any(_texto(t) is None for t in turnos):
        return "turnos"
    historial = payload.get("history") or []
    if not isinstance(historial, list) or len(historial) > MAX_HISTORIAL:
        return "historial"
    for m in historial:
        if not isinstance(m, dict) or m.get("role") not in ("customer", "agent") or _texto(m.get("text")) is None:
            return "historial"
    return None


@router.post("/vocero/ensayo")
async def recibir(request: Request) -> Any:
    """Corre el ensayo en la misma petición y devuelve lo que contestó el agente.

    Síncrono a propósito: no hay cola porque no hay nada que entregar después.
    Si la petición se corta, el ensayo se pierde entero, y eso es lo correcto:
    no dejó nada escrito en ningún lado.
    """
    ctx: AppContext = request.app.state.ctx
    if not ctx.settings.multi_org or ctx.registro is None:
        # Solo Vocero Cloud ensaya así: el raíz tiene su Laboratorio.
        return JSONResponse({"error": "no disponible"}, status_code=404)
    body = await limited_body(request)
    if not firma_valida(body, request.headers.get("x-vocero-signature"), ctx.settings.dispatch_secret):
        logger.warning("ensayo con firma inválida o ausente — 401")
        return JSONResponse({"error": "firma inválida"}, status_code=401)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "cuerpo ilegible"}, status_code=400)
    motivo = validar(payload)
    if motivo is not None:
        return JSONResponse({"error": "ensayo inválido", "campo": motivo}, status_code=422)
    return await ensayar(ctx, payload)


async def ensayar(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    org_id, slug = organizacion_del_despacho(payload)  # type: ignore[misc]
    contexto: dict[str, Any] = json.loads(json.dumps(payload["context"]))
    ficha = str(payload["rehearsal"]["token"])
    capacidades = [str(c) for c in payload.get("capabilities") or []]
    conversation_id = str(contexto["conversation"]["id"])
    identity = str(contexto["contact"]["identity"])
    nombre = contexto["contact"].get("name") or "Cliente de prueba"
    # Un ensayo siempre empieza con la conversación abierta y la IA encendida:
    # se prueba al agente, no el estado de una conversación real.
    contexto["conversation"].update(aiEnabled=True, windowOpen=True)

    registro = ctx.registro
    lectura = BrainsCrmClient(
        "", "", slug, client=registro.http_de_ensayo(org_id, slug, ficha)
    )
    crm = CrmDeEnsayo(contexto, lectura, capacidades)
    config_ia = contexto.get("llm")
    if not isinstance(config_ia, dict) or not config_ia.get("model"):
        await crm.aclose()
        # Mismo criterio que el despacho: sin la IA del negocio no se piensa.
        return {"ok": False, "error": "sin_credencial", "turns": []}
    llm = registro.llm_de_ensayo(org_id, slug, config_ia, ctx.settings, ficha)

    store = MemoryStore()
    ctx_ensayo = replace(
        ctx,
        store=store,
        crm=crm,
        llm=llm,
        profile=ProfileProvider(crm, default_name=ctx.settings.agent_name),
        organizacion=(org_id, slug),
        agenda_enabled=contexto.get("booking") is not None,
        agenda_sonda=None,
        turn_locks={},
        turn_lock_users={},
    )

    # El historial que ya traía la ventana de chat entra a la memoria del
    # ensayo, no a la de Nea: así el turno nuevo lo lee como cualquier otro.
    conv = await store.get_or_create_conversation(identity, org_id, slug)
    historial = payload.get("history") or []
    for m in historial:
        await store.add_message(conv.id, "user" if m["role"] == "customer" else "assistant", _texto(m["text"]) or "")
    await store.update_conversation(
        conv.id,
        crm_conversation_id=conversation_id,
        greeted=any(m["role"] == "agent" for m in historial),
    )

    resultado: list[dict[str, Any]] = []
    try:
        for i, texto in enumerate(payload["turns"]):
            desde = len(crm.eventos)
            mensaje = InboundMessage(
                wa_message_id=f"ensayo_{secrets.token_hex(6)}",
                identity=identity,
                type="text",
                text=_texto(texto),
                profile_name=nombre,
                channel=str(contexto["conversation"].get("channel") or "whatsapp"),
                # Lo mismo que el Laboratorio: sin lista de permitidos (no hay
                # una persona real del otro lado) y sin seguimiento.
                prueba=True,
            )
            await handle_flush(ctx_ensayo, identity, [mensaje], crm_conversation_id=conversation_id)
            eventos = crm.eventos[desde:]
            resultado.append(
                {
                    "customer": _texto(texto),
                    "replies": [e["text"] for e in eventos if e["type"] == "message"],
                    "actions": [e for e in eventos if e["type"] != "message"],
                }
            )
    finally:
        await crm.aclose()
        await llm._client.close()

    uso = getattr(llm, "usage", {}) or {}
    logger.info(
        "ensayo de %s: %d turno(s), %d llamada(s) al modelo",
        slug,
        len(resultado),
        uso.get("llamadas", 0),
    )
    return {
        "ok": True,
        "simulated": True,
        "model": config_ia.get("model"),
        "usage": {
            "llmCalls": uso.get("llamadas", 0),
            "promptTokens": uso.get("prompt", 0),
            "completionTokens": uso.get("completion", 0),
        },
        "turns": resultado,
    }

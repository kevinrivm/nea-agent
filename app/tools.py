"""Herramientas del LLM: update_ficha, propose_slots, book_session, route_out, handoff.

Solo se reserva lo que se ofreció, y **quien manda sobre eso es el CRM**:
Vocero guarda la oferta contra la conversación y rechaza cualquier otro
instante. La tabla `offered_slots` de Nea es un ESPEJO de esa oferta, no una
segunda fuente de verdad: sirve para etiquetar con el día en palabras y para
frenar una alucinación antes de gastar un viaje de red. Si el CRM dice que un
horario no se ofreció, el espejo está viejo y se resincroniza con lo que él
mande.

Un fallo del CRM dentro de una tool regresa `{"ok": false, ...}` al LLM —
nunca tumba el turno.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.crm import (
    AgendaUnavailable,
    CrmConflict,
    CrmError,
    SlotNotOffered,
    SlotTaken,
)
from app.profile import BusinessProfile
from app.state import AppContext, Conversation, OfferedSlot

logger = logging.getLogger("nea.tools")

# Cuántos huecos quedan RESERVABLES tras un propose_slots. El agente muestra 3
# a la vez (regla del prompt), pero guardar solo 3 lo dejaba sin nada que
# ofrecer cuando el lead pedía otro día: el catálogo reservable es más ancho
# que el menú que se enseña.
MAX_OFFERED = 12
# Reparto pedido al CRM: hasta 3 huecos por día, en 5 días distintos.
OFFER_PER_DAY = 3
OFFER_DAYS = 5

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "update_ficha",
            "description": (
                "Guarda o actualiza la ficha del lead en el CRM (merge: solo los "
                "campos que mandes). Llámala en cuanto descubras un dato nuevo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rubro": {"type": "string"},
                    "rol": {
                        "type": "string",
                        "description": "dueno | hijo_del_dueno | empleado | otro",
                    },
                    "tamano_aprox": {"type": "string"},
                    "sistemas": {"type": "string"},
                    "dolor_principal": {"type": "string"},
                    "geo": {"type": "string"},
                    "calificado": {"type": "boolean"},
                    "resultado": {
                        "type": "string",
                        "description": "agendo | dio_diy | handoff | sin_respuesta",
                    },
                    "notas": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_slots",
            "description": (
                "Consulta la disponibilidad real de la agenda del negocio. Te "
                "regresa los huecos libres REPARTIDOS entre los próximos días, "
                "cada uno con su día en palabras (hoy/mañana/nombre del día). "
                "Ofrece al lead máximo 3, los que embonen con lo que pidió. Si "
                "el día que pidió no aparece, es que no hay agenda ese día: "
                "dilo. SOLO estos horarios serán reservables después."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_session",
            "description": (
                "Reserva la cita en uno de los horarios previamente ofrecidos. "
                "start_utc debe ser EXACTAMENTE el start_utc de un slot ofrecido "
                "en esta conversación. Llámala SOLO después de haber nombrado el "
                "día completo y de que el lead lo aceptara sin ambigüedad."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC del slot elegido, tal cual se ofreció",
                    },
                    "dia_confirmado": {
                        "type": "string",
                        "description": (
                            "Lo que el lead escribió para aceptar ESE día concreto. "
                            "Si no puedes citarlo, todavía no confirmó: pregunta "
                            "en vez de reservar."
                        ),
                    },
                },
                "required": ["start_utc", "dia_confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_session",
            "description": (
                "Mueve la cita YA agendada del lead a otro horario ofrecido. "
                "Mismo protocolo que book_session: primero propose_slots, luego "
                "confirmas el día completo, y hasta entonces mueves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC del nuevo slot, tal cual se ofreció",
                    },
                    "dia_confirmado": {
                        "type": "string",
                        "description": "Lo que el lead escribió para aceptar ESE día",
                    },
                },
                "required": ["start_utc", "dia_confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "route_out",
            "description": (
                "Marca al lead como no calificado (hoy). Después despídete con "
                "honestidad, compartiendo los recursos alternativos del negocio "
                "si existen, puerta abierta."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff",
            "description": (
                "Pasa la conversación a un humano del negocio y pausa la IA. Tu "
                "mensaje de despedida se envía ANTES de la pausa — salvo en el "
                "handoff por hostilidad, donde cierras sobrio sin anunciarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo breve (p.ej. 'pidió humano', 'duda fuera del conocimiento')",
                    }
                },
            },
        },
    },
]


# Herramientas que solo tienen sentido si el CRM agenda.
AGENDA_TOOLS = frozenset({"propose_slots", "book_session", "reschedule_session"})


def tool_schemas(agenda_enabled: bool = True) -> list[dict[str, Any]]:
    """El catálogo que se le ofrece al modelo en ESTE turno.

    Contra un CRM sin agenda no se le enseñan las herramientas de agendar: si
    se le enseñan, las llama, fallan todas y el lead recibe evasivas en vez de
    un handoff limpio. Que no exista la herramienta es más claro que pedirle al
    prompt que se acuerde de no usarla.
    """
    if agenda_enabled:
        return TOOL_SCHEMAS
    return [
        t
        for t in TOOL_SCHEMAS
        if t.get("function", {}).get("name") not in AGENDA_TOOLS
    ]


def _meeting(result: dict[str, Any]) -> tuple[str | None, bool]:
    """Enlace de la reunión y si el CRM lo dejó pendiente.

    Vocero devuelve `meetingLink` desde que la entrega de la reunión es un
    conector (puede ser Zoom, Google Meet o la sala fija del negocio); antes
    era `zoomJoinUrl`, y ese nombre se sigue aceptando para no romper un CRM
    viejo. Leer solo el viejo hacía que el enlace llegara SIEMPRE vacío contra
    un Vocero actual: la cita se creaba bien y el lead se quedaba sin por dónde
    entrar.

    `linkPending` es lo que evita prometer de más: la cita existe pero el
    proveedor todavía no entregó el enlace, así que se confirma la cita y se
    dice que el enlace llega en un momento.
    """
    link = result.get("meetingLink") or result.get("zoomJoinUrl")
    pending = bool(result.get("linkPending"))
    return (str(link) if link else None), pending


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _label_of(raw: dict[str, Any], start: datetime) -> str:
    """Etiqueta con el día en palabras: "hoy viernes 7 de agosto, 10:30".

    La corta del CRM ("vie 7 ago, 10:30") se presta a que el lead entienda
    otro día: basta que conteste "10:30, de mañana" a una oferta de HOY para
    agendar mal. Si el CRM no manda `dayLabel` (respuestas sin reparto, p. ej.
    las alternativas de un slot_taken), se cae a la corta.
    """
    day_label = str(raw.get("dayLabel") or "").strip()
    time = str(raw.get("time") or "").strip()
    if day_label and time:
        return f"{day_label}, {time}"
    return str(raw.get("label") or _iso_z(start))


def _slots_from_payload(
    conversation_id: int, raw_slots: list[dict[str, Any]]
) -> list[OfferedSlot]:
    """Convierte slots del CRM ({startUtc,endUtc,label}) a OfferedSlot, tolerante."""
    out: list[OfferedSlot] = []
    for raw in raw_slots[:MAX_OFFERED]:
        start = _parse_utc(str(raw.get("startUtc") or ""))
        if start is None:
            continue
        end = _parse_utc(str(raw.get("endUtc") or "")) if raw.get("endUtc") else None
        out.append(
            OfferedSlot(
                conversation_id=conversation_id,
                start_utc=start,
                end_utc=end,
                label=_label_of(raw, start),
            )
        )
    return out


def _slots_for_llm(slots: list[OfferedSlot]) -> list[dict[str, str]]:
    return [{"start_utc": _iso_z(s.start_utc), "label": s.label} for s in slots]


class ToolRuntime:
    """Ejecuta las tool-calls de UN turno y acumula sus efectos."""

    def __init__(
        self,
        ctx: AppContext,
        conv: Conversation,
        crm_conversation_id: str,
        profile: BusinessProfile | None = None,
    ) -> None:
        self._ctx = ctx
        self._conv = conv
        self._crm_conv_id = crm_conversation_id
        self._profile = profile or BusinessProfile()
        # Efectos observables por turn.py:
        self.handoff_reason: str | None = None  # se ejecuta DESPUÉS de la despedida
        self.booked = False
        self.routed_out = False
        self.proposed = False

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "update_ficha":
                return await self._update_ficha(args)
            if name == "propose_slots":
                return await self._propose_slots()
            if name == "book_session":
                return await self._book_session(args)
            if name == "reschedule_session":
                return await self._reschedule_session(args)
            if name == "route_out":
                return await self._route_out()
            if name == "handoff":
                return self._handoff(args)
            logger.warning("tools: herramienta desconocida %r", name)
            return {"ok": False, "error": f"herramienta desconocida: {name}"}
        except CrmError as exc:
            logger.warning("tools: %s falló contra el CRM: %s", name, exc)
            return {
                "ok": False,
                "error": "crm_error",
                "detalle": "no pude completar la acción; continúa la conversación o haz handoff",
            }

    async def _update_ficha(self, args: dict[str, Any]) -> dict[str, Any]:
        # Tolera el drift del LLM: manda lo que haya, el CRM normaliza flojo.
        ficha = {k: v for k, v in args.items() if v is not None}
        if not ficha:
            return {"ok": True, "nota": "sin campos nuevos"}
        await self._ctx.crm.put_ficha(self._crm_conv_id, ficha)
        return {"ok": True}

    async def _propose_slots(self) -> dict[str, Any]:
        # La conversación va SIEMPRE: es contra ella que el CRM registra la
        # oferta, y sin ella no hay nada reservable después.
        try:
            raw = await self._ctx.crm.get_availability(
                self._crm_conv_id,
                limit=MAX_OFFERED,
                per_day=OFFER_PER_DAY,
                days=OFFER_DAYS,
            )
        except AgendaUnavailable:
            return self._sin_agenda()
        slots = _slots_from_payload(self._conv.id, raw)
        if not slots:
            return {
                "ok": False,
                "error": "sin_disponibilidad",
                "detalle": "no hay horarios abiertos; ofrece handoff para coordinar directo",
            }
        await self._ctx.store.replace_offered_slots(self._conv.id, slots)
        self.proposed = True
        return {
            "ok": True,
            "slots": _slots_for_llm(slots),
            "dias_con_agenda": sorted(
                {s.label.rsplit(",", 1)[0].strip() for s in slots}
            ),
            "instrucciones": (
                "esta es TODA la agenda abierta: los días que no aparecen aquí "
                "NO tienen agenda, dilo en vez de mover al lead a otro día. "
                "Ofrécele máximo 3, con su etiqueta tal cual (día incluido), "
                "los que embonen con lo que pidió."
            ),
        }

    async def _resolve_offered(
        self, args: dict[str, Any], accion: str
    ) -> tuple[OfferedSlot | None, dict[str, Any] | None]:
        """Slot elegido, o el error listo para devolverle al LLM.

        Validación server-side por epoch exacto: solo lo ofrecido es reservable.
        """
        wanted = _parse_utc(str(args.get("start_utc") or ""))
        offered = await self._ctx.store.get_offered_slots(self._conv.id)
        if wanted is None:
            return None, {
                "ok": False,
                "error": "start_utc_invalido",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        chosen = next(
            (
                s
                for s in offered
                if int(s.start_utc.timestamp()) == int(wanted.timestamp())
            ),
            None,
        )
        if chosen is None:
            logger.info(
                "tools: %s rechazado — %s no está entre los ofrecidos",
                accion,
                args.get("start_utc"),
            )
            return None, {
                "ok": False,
                "error": "slot_no_ofrecido",
                "detalle": "solo puedes agendar un horario que ya ofreciste",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        # Deja rastro de sobre qué frase del lead se tomó la decisión: cuando
        # una cita sale mal, esto dice si hubo confirmación o se asumió.
        logger.info(
            "tools: %s a %s (el lead confirmó con: %r)",
            accion,
            chosen.label,
            str(args.get("dia_confirmado") or "")[:120],
        )
        return chosen, None

    def _sin_agenda(self) -> dict[str, Any]:
        """Este CRM no tiene agenda: dejar de prometer citas, no reintentar."""
        self._ctx.agenda_enabled = False
        logger.info("tools: el CRM no expone agenda — agendamiento desactivado")
        return {
            "ok": False,
            "error": "sin_agenda",
            "detalle": (
                "este negocio no agenda por aquí; no ofrezcas horarios ni "
                "prometas cita — resuelve lo que puedas y haz handoff"
            ),
        }

    async def _resync_offer(
        self, exc: SlotNotOffered, accion: str
    ) -> dict[str, Any]:
        """El CRM no reconoce ese horario: su lista manda, la nuestra se tira.

        Pasa cuando el espejo local quedó viejo — por ejemplo si el CRM
        reemplazó la oferta por su cuenta. Antes esto caía en el `except
        CrmError` genérico y el agente solo decía "no pude"; ahora vuelve a
        ofrecer lo que el CRM sí tiene registrado.
        """
        fresh = _slots_from_payload(self._conv.id, exc.slots)
        await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
        logger.info(
            "tools: %s rechazado por el CRM (no ofrecido) — oferta resincronizada a %d",
            accion,
            len(fresh),
        )
        if not fresh:
            return {
                "ok": False,
                "error": "slot_no_ofrecido",
                "detalle": (
                    "el CRM no tiene horarios ofrecidos en esta conversación; "
                    "vuelve a llamar propose_slots antes de agendar"
                ),
            }
        return {
            "ok": False,
            "error": "slot_no_ofrecido",
            "detalle": (
                "ese horario ya no está ofrecido; ofrécele estos, que son los "
                "que el negocio tiene reservados para esta conversación"
            ),
            "slots": _slots_for_llm(fresh),
        }

    async def _book_session(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offered(args, "book_session")
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "slot_no_ofrecido"}
        try:
            result = await self._ctx.crm.create_booking(
                self._crm_conv_id, _iso_z(chosen.start_utc)
            )
        except SlotTaken as exc:
            # El slot se ocupó entre oferta y elección: alternativas frescas.
            fresh = _slots_from_payload(self._conv.id, exc.slots)
            await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
            return {
                "ok": False,
                "error": "slot_taken",
                "detalle": "ese horario se acaba de ocupar; discúlpate breve y ofrece estas alternativas",
                "slots": _slots_for_llm(fresh),
            }
        except SlotNotOffered as exc:
            return await self._resync_offer(exc, "book_session")
        except AgendaUnavailable:
            return self._sin_agenda()
        await self._ctx.store.clear_offered_slots(self._conv.id)
        self.booked = True
        try:
            await self._ctx.crm.put_ficha(
                self._crm_conv_id, {"calificado": True, "resultado": "agendo"}
            )
        except CrmError as exc:  # best-effort: la cita ya existe
            logger.warning("tools: no pude actualizar ficha tras booking: %s", exc)
        return {
            "ok": True,
            # La etiqueta del slot ofrecido trae el día en palabras; la del
            # CRM es la corta. Se repite ESTA para que el lead lea el día.
            "label": chosen.label or result.get("label"),
            "meeting_url": _meeting(result)[0],
            "enlace_pendiente": _meeting(result)[1],
            "instrucciones": (
                "confirma el día COMPLETO y la hora tal cual dice label, "
                "comparte meeting_url si viene y menciona lo que el negocio "
                "pida para llegar preparado. Si enlace_pendiente es true, la "
                "cita SÍ quedó: di que el enlace le llega por aquí en un "
                "momento, no prometas uno que no tienes"
            ),
        }

    async def _reschedule_session(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offered(args, "reschedule_session")
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "slot_no_ofrecido"}
        try:
            result = await self._ctx.crm.reschedule_booking(
                self._crm_conv_id, _iso_z(chosen.start_utc)
            )
        except SlotTaken as exc:
            fresh = _slots_from_payload(self._conv.id, exc.slots)
            await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
            return {
                "ok": False,
                "error": "slot_taken",
                "detalle": "ese horario se acaba de ocupar; discúlpate breve y ofrece estas alternativas",
                "slots": _slots_for_llm(fresh),
            }
        except SlotNotOffered as exc:
            return await self._resync_offer(exc, "reschedule_session")
        except AgendaUnavailable:
            return self._sin_agenda()
        except CrmConflict as exc:
            if exc.code == "no_booking":
                return {
                    "ok": False,
                    "error": "sin_cita",
                    "detalle": "el lead no tiene cita por delante; usa book_session",
                }
            raise
        await self._ctx.store.clear_offered_slots(self._conv.id)
        self.booked = True
        return {
            "ok": True,
            "label": chosen.label or result.get("label"),
            "meeting_url": _meeting(result)[0],
            "enlace_pendiente": _meeting(result)[1],
            "instrucciones": (
                "confirma que quedó movida, con el día COMPLETO y la hora tal "
                "cual dice label; el link de la videollamada sigue siendo el "
                "mismo salvo que aquí venga otro"
            ),
        }

    async def _route_out(self) -> dict[str, Any]:
        # "dio_diy" es el valor del enum `resultado` en el gateway del CRM
        # (006); el nombre de la herramienta es genérico, el cable no cambia.
        await self._ctx.crm.put_ficha(
            self._crm_conv_id, {"calificado": False, "resultado": "dio_diy"}
        )
        self.routed_out = True
        out: dict[str, Any] = {"ok": True}
        if self._profile.resources:
            out["recursos"] = self._profile.resources
            out["instrucciones"] = "comparte estos recursos al despedirte, puerta abierta"
        return out

    def _handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        self.handoff_reason = str(args.get("reason") or "lead_request")
        return {
            "ok": True,
            "nota": (
                "el pase a humano se ejecutará después de tu mensaje de despedida"
            ),
        }

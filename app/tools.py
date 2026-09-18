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
from datetime import date, datetime, timezone
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
# Con `fecha`, el CRM ofrece TODAS las horas de ese día (hasta 24). El espejo
# tiene que guardarlas todas: si guardara 12, Nea rechazaría por su cuenta la
# hora 13 que el CRM sí ofreció.
MAX_OFFERED_DIA = 24
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
                "Consulta la disponibilidad real de la agenda del negocio. Sin "
                "fecha te regresa un REPARTO: unas horas de cada uno de los "
                "próximos días, con su día en palabras (hoy/mañana/nombre del "
                "día). Con fecha te regresa TODAS las horas libres de ese día, "
                "o por qué no hay (cerrado, lleno, aún sin agenda). Si el lead "
                "pide un día u hora que no viene en el reparto, consúltalo con "
                "fecha antes de contestar. SOLO los horarios de la última "
                "consulta con resultados serán reservables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha": {
                        "type": "string",
                        "description": (
                            "Opcional. Día concreto en AAAA-MM-DD, calculado con "
                            "la fecha de hoy del contexto (\"el jueves de la "
                            "próxima semana\" → su fecha)."
                        ),
                    }
                },
            },
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
                    "recordatorios_aceptados": {
                        "type": "boolean",
                        "description": (
                            "true SOLO si el lead autorizó explícitamente recibir "
                            "recordatorios de ESTA cita. Reservar o confirmar el "
                            "horario no implica permiso. Si no lo dijo o lo rechazó, false."
                        ),
                    },
                },
                "required": ["start_utc", "dia_confirmado", "recordatorios_aceptados"],
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


AGENDA_V2_SCHEMAS = [
    {"type": "function", "function": {"name": "list_bookings", "description": "Consulta las citas activas de esta conversación. Muestra sus etiquetas y pide elegir y confirmar antes de mover o cancelar. Nunca inventes una selección.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "cancel_session", "description": "Cancela la cita seleccionada SOLO después de confirmación explícita del cliente. Usa el selectionToken devuelto por list_bookings.", "parameters": {"type": "object", "properties": {"selection_token": {"type": "string"}, "confirmation": {"type": "boolean"}}, "required": ["selection_token", "confirmation"]}}},
]

# Herramientas que solo tienen sentido si el CRM agenda.
AGENDA_TOOLS = frozenset({"propose_slots", "book_session", "reschedule_session"})


def tool_schemas(agenda_enabled: bool = True, agenda_v2: bool = False, coordination: bool = False) -> list[dict[str, Any]]:
    """El catálogo que se le ofrece al modelo en ESTE turno.

    Contra un CRM sin agenda no se le enseñan las herramientas de agendar: si
    se le enseñan, las llama, fallan todas y el lead recibe evasivas en vez de
    un handoff limpio. Que no exista la herramienta es más claro que pedirle al
    prompt que se acuerde de no usarla.
    """
    if agenda_enabled:
        if agenda_v2:
            import copy
            schemas = copy.deepcopy(TOOL_SCHEMAS)
            for tool in schemas:
                if tool["function"]["name"] == "reschedule_session":
                    params = tool["function"]["parameters"]
                    params["properties"]["selection_token"] = {"type": "string", "description": "Token de list_bookings de la cita elegida y confirmada por el cliente"}
                    params["required"].append("selection_token")
            return schemas + AGENDA_V2_SCHEMAS + ([{"type": "function", "function": {"name": "coordination_consent", "description": "Registra consentimiento EXPLÍCITO para un único recordatorio de coordinación tras 24 horas sin reservar. Nunca deduzcas consentimiento por pedir una cita. consent=false si rechaza seguimiento. Solo tras ofrecer horarios.", "parameters": {"type": "object", "properties": {"consent": {"type": "boolean"}}, "required": ["consent"]}}}] if coordination else [])
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


def _fecha_pedida(value: Any) -> str | None:
    """AAAA-MM-DD válida, o None. El modelo a veces manda "jueves" o "17/09"."""
    texto = str(value or "").strip()
    try:
        return date.fromisoformat(texto).isoformat() if len(texto) == 10 else None
    except ValueError:
        return None


def _cobertura(query: dict[str, Any] | None) -> dict[str, Any]:
    """Qué decirle al modelo de lo que NO viene en el reparto.

    Antes se le decía "esta es TODA la agenda: los días que no aparecen NO
    tienen agenda". No era cierto —el reparto son unas horas de unos cuantos
    días— y en Tobaxis sonó así: "la próxima semana jueves o viernes ya no
    tienen agenda" (no se habían consultado) y "el jueves a las 11 no hay"
    (solo veía las 3 primeras horas del día).
    """
    base = (
        "Ofrécele máximo 3, con su etiqueta tal cual (día incluido), los que "
        "embonen con lo que pidió. Esta lista es un REPARTO, no toda la agenda: "
        "si pide un día u hora que no ves aquí, llama propose_slots con "
        "fecha=AAAA-MM-DD ANTES de contestarle. Nunca digas que un día u hora "
        "no tiene agenda sin haber consultado ese día."
    )
    if not query:
        return {"instrucciones": base}
    out: dict[str, Any] = {"instrucciones": base}
    if query.get("coveredUntil"):
        out["revisado_hasta"] = query["coveredUntil"]
        out["instrucciones"] += (
            f" Los días posteriores al {query['coveredUntil']} NO se revisaron."
        )
    if query.get("perDay"):
        out["instrucciones"] += (
            f" De cada día solo ves hasta {query['perDay']} horas; puede haber más."
        )
    if query.get("horizonEnd"):
        out["se_agenda_hasta"] = query["horizonEnd"]
    return out


def _slots_from_payload(
    conversation_id: int,
    raw_slots: list[dict[str, Any]],
    limit: int = MAX_OFFERED,
) -> list[OfferedSlot]:
    """Convierte slots del CRM ({startUtc,endUtc,label}) a OfferedSlot, tolerante."""
    out: list[OfferedSlot] = []
    for raw in raw_slots[:limit]:
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
        self.booking_confirmation: dict[str, Any] | None = None
        self.routed_out = False
        self.proposed = False

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "coordination_consent" and getattr(self._ctx.crm, "supports_coordination", False):
                if not isinstance(args.get("consent"), bool):
                    return {"ok": False, "error": "explicit_consent_required"}
                return await self._ctx.crm.set_coordination_consent(self._crm_conv_id, args["consent"])
            if name == "list_bookings" and getattr(self._ctx.crm, "supports_agenda_v2", False):
                return {"ok": True, "bookings": await self._ctx.crm.list_bookings(self._crm_conv_id)}
            if name == "cancel_session" and getattr(self._ctx.crm, "supports_agenda_v2", False):
                if args.get("confirmation") is not True or not args.get("selection_token"):
                    return {"ok": False, "error": "confirmation_required"}
                return await self._ctx.crm.cancel_booking(self._crm_conv_id, str(args["selection_token"]), True)
            if name == "update_ficha":
                return await self._update_ficha(args)
            if name == "propose_slots":
                return await self._propose_slots(args)
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

    async def _propose_slots(self, args: dict[str, Any]) -> dict[str, Any]:
        fecha = _fecha_pedida(args.get("fecha"))
        if args.get("fecha") and fecha is None:
            return {
                "ok": False,
                "error": "fecha_invalida",
                "detalle": "fecha va como AAAA-MM-DD (p. ej. 2026-09-17); vuelve a llamar",
            }
        # La conversación va SIEMPRE: es contra ella que el CRM registra la
        # oferta, y sin ella no hay nada reservable después.
        try:
            consulta = await self._ctx.crm.consultar_huecos(
                self._crm_conv_id,
                date=fecha,
                limit=MAX_OFFERED,
                per_day=OFFER_PER_DAY,
                days=OFFER_DAYS,
            )
        except AgendaUnavailable:
            return self._sin_agenda()
        query = consulta.get("query") if isinstance(consulta.get("query"), dict) else None
        if fecha:
            return await self._huecos_del_dia(fecha, consulta["slots"], query)

        slots = _slots_from_payload(self._conv.id, consulta["slots"])
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
            **_cobertura(query),
        }

    async def _huecos_del_dia(
        self, fecha: str, raw: list[dict[str, Any]], query: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Un día concreto: todas sus horas, o por qué no hay ninguna.

        Un CRM que no conoce `date` la ignora y devuelve el reparto de
        siempre; eso se nota porque no regresa `query.date`. Entonces el día
        NO se consultó, y decirle al lead "ese día no hay" sería mentirle.
        """
        if not query or query.get("date") != fecha:
            return {
                "ok": False,
                "error": "consulta_por_dia_no_disponible",
                "detalle": (
                    f"no pude revisar el {fecha} en específico. NO digas que ese "
                    "día no hay agenda: ofrécele los horarios que ya le diste o "
                    "handoff para coordinarlo directo"
                ),
            }
        status = query.get("status")
        slots = _slots_from_payload(self._conv.id, raw, limit=MAX_OFFERED_DIA)
        if status == "available" and slots:
            horas = [s.label.rsplit(",", 1)[-1].strip() for s in slots]
            await self._ctx.store.replace_offered_slots(self._conv.id, slots)
            self.proposed = True
            return {
                "ok": True,
                "fecha": fecha,
                # La lista corta de horas va aparte a propósito: en la
                # autoprueba, con las 11:00 dentro de `slots`, el modelo
                # contestó "a las 11 no tengo espacio". Leer "11:00" en una
                # lista de horas no se presta a esa confusión.
                "horas_libres": horas,
                "slots": _slots_for_llm(slots),
                "instrucciones": (
                    f"horas libres del {fecha} (hora del negocio): "
                    f"{', '.join(horas)}. Si la hora que pidió ESTÁ en esa "
                    "lista, SÍ está libre: ofrécesela. Si no está, dilo y "
                    "ofrécele las más cercanas de esa lista, con su etiqueta tal "
                    "cual. Máximo 3. Para reservar usa el start_utc del slot "
                    "(viene en UTC, no se lo digas al lead)."
                ),
            }
        # Sin horas ese día. La oferta anterior se conserva (igual que en el
        # CRM): lo que ya se le ofreció sigue siendo reservable.
        motivo = {
            "closed": f"el {fecha} el negocio no abre",
            "full": f"el {fecha} ya no quedan horarios libres",
            "past": f"el {fecha} ya pasó; confirma qué día quiso decir",
            "beyond_horizon": (
                f"todavía no se abre agenda para el {fecha} (se agenda hasta el "
                f"{query.get('horizonEnd')}). Dilo así — no es que esté lleno — y "
                "ofrécele lo más lejano que sí haya o que te escriba más cerca de la fecha"
            ),
        }.get(str(status), f"el {fecha} no tiene horarios libres")
        return {
            "ok": False,
            "error": "dia_sin_horarios",
            "fecha": fecha,
            "estado": status,
            "detalle": (
                f"{motivo}. Díselo derecho y ofrécele otro día — NUNCA acomodes "
                "su petición en otro día como si fuera lo mismo."
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
            consent = args.get("recordatorios_aceptados") is True
            if getattr(self._ctx.crm, "supports_agenda_v2", False):
                result = await self._ctx.crm.create_booking(
                    self._crm_conv_id, _iso_z(chosen.start_utc), reminder_consent=consent
                )
            else:
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
        meeting_url, link_pending = _meeting(result)
        self.booking_confirmation = {
            "label": chosen.label or result.get("label"),
            "meeting_url": meeting_url,
            "link_pending": link_pending,
            "reminder_consent": bool(result.get("reminderConsent", False)),
        }
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
            "meeting_url": meeting_url,
            "enlace_pendiente": link_pending,
            "recordatorios_activados": bool(result.get("reminderConsent", False)),
            "instrucciones": (
                "confirma el día COMPLETO y la hora tal cual dice label, "
                "comparte meeting_url si viene y menciona lo que el negocio "
                "pida para llegar preparado. Si enlace_pendiente es true, la "
                "cita SÍ quedó: di que el enlace le llega por aquí en un "
                "momento, no prometas uno que no tienes"
            ),
        }

    def finalize_reply(self, text: str) -> str:
        """Booking confirmation is authoritative, not left to model wording."""
        data = self.booking_confirmation
        if not data:
            return text
        label = data.get("label") or "el horario acordado"
        parts = [f"Listo, tu cita quedó confirmada para {label}."]
        if data.get("meeting_url"):
            parts.append(f"Enlace de Zoom: {data['meeting_url']}")
        elif data.get("link_pending"):
            parts.append("El enlace de la videollamada te llegará por aquí en un momento.")
        if data.get("reminder_consent"):
            parts.append("También quedaron activados los recordatorios que autorizaste.")
        return "\n\n".join(parts)

    async def _reschedule_session(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offered(args, "reschedule_session")
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "slot_no_ofrecido"}
        try:
            if getattr(self._ctx.crm, "supports_agenda_v2", False):
                if not args.get("selection_token"):
                    return {"ok": False, "error": "selection_required", "detalle": "consulta list_bookings y pide elegir la cita antes de mover"}
                result = await self._ctx.crm.reschedule_booking(self._crm_conv_id, _iso_z(chosen.start_utc), selection_token=str(args["selection_token"]))
            else:
                result = await self._ctx.crm.reschedule_booking(self._crm_conv_id, _iso_z(chosen.start_utc))
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

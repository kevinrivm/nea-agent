"""Estado propio del bot: modelos, contrato de almacenamiento y fake en memoria.

El acceso a datos está abstraído en el protocolo `Store` para que los unit
tests corran con `MemoryStore` (sin Postgres). La implementación real con
asyncpg vive en `app/db.py` (`PgStore`).
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.config import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- modelos ---


@dataclass
class Conversation:
    id: int
    wa_identity: str
    crm_conversation_id: str | None = None
    # De quién es esta conversación. Cadena vacía = instalación de un solo
    # negocio, que es lo que era todo antes del modo multi-organización.
    #
    # Importa más de lo que parece: la identidad SOLA dejó de identificar una
    # conversación en cuanto una Nea atiende a varios negocios, porque la
    # misma persona puede escribirle a dos.
    organization_id: str = ""
    organization_slug: str = ""
    phase: str = "descubrimiento"  # descubrimiento|insight|salida|agendando|cerrada
    greeted: bool = False
    media_notice_sent: bool = False
    followup_due_at: datetime | None = None
    followup_sent: bool = False
    last_inbound_at: datetime | None = None
    # Puesta cuando el agente cierra por conversación sin rumbo: mientras
    # viva, el turno guarda silencio (ver app/stall.py).
    stalled_at: datetime | None = None


@dataclass
class BotMessage:
    id: int
    conversation_id: int
    role: str  # user | assistant
    content: str
    wa_message_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class OfferedSlot:
    conversation_id: int
    start_utc: datetime
    end_utc: datetime | None
    label: str
    offered_at: datetime = field(default_factory=utcnow)


@dataclass
class RelayItem:
    id: int
    body: bytes
    signature: str | None
    attempts: int
    created_at: datetime
    next_retry_at: datetime
    delivered_at: datetime | None = None
    abandoned_at: datetime | None = None


@dataclass
class PendingSend:
    """Respuesta generada cuyo envío al CRM agotó los reintentos del turno.
    Se persiste para reintento diferido — jamás se descarta (incidente 2026-08-03)."""

    id: int
    conversation_id: int
    crm_conversation_id: str
    content: str
    attempts: int
    created_at: datetime
    next_retry_at: datetime
    delivered_at: datetime | None = None
    abandoned_at: datetime | None = None
    # Para que el reintento diferido sepa con qué credencial hablarle al CRM
    # cuando despierte. Van al final por la regla de los dataclass: un campo
    # con valor por defecto no puede preceder a uno sin él.
    organization_id: str = ""
    organization_slug: str = ""
    # Y qué despacho estaba contestando: el CRM lo exige al responder por el
    # cerebro, y cuando el worker despierta el turno ya no existe para dárselo.
    dispatch_id: str = ""


@dataclass
class InboundMessage:
    """Mensaje entrante ya parseado del webhook de Meta."""

    wa_message_id: str | None
    identity: str
    type: str
    text: str | None = None
    referral_headline: str | None = None
    profile_name: str | None = None
    # Multimedia (spec 002): lo mínimo para procesar el contenido.
    media_id: str | None = None
    media_mime: str | None = None
    media_filename: str | None = None
    media_caption: str | None = None
    media_voice: bool = False
    location: dict[str, Any] | None = None
    contact_names: list[str] = field(default_factory=list)


# --------------------------------------------------------------- contrato ---


class Store(Protocol):
    """Contrato de persistencia del bot (Postgres real o memoria en tests)."""

    # dedup
    async def mark_processed(self, wa_message_id: str) -> bool:
        """True si el mensaje es nuevo (gana el INSERT); False si ya se procesó."""
        ...

    # cola de relay
    async def enqueue_relay(self, body: bytes, signature: str | None) -> int: ...
    async def due_relays(self, now: datetime) -> list[RelayItem]: ...
    async def mark_relay_delivered(self, relay_id: int) -> None: ...
    async def mark_relay_abandoned(self, relay_id: int) -> None: ...
    async def reschedule_relay(
        self, relay_id: int, attempts: int, next_retry_at: datetime
    ) -> None: ...

    # conversaciones
    async def get_or_create_conversation(
        self,
        wa_identity: str,
        organization_id: str = "",
        organization_slug: str = "",
    ) -> Conversation: ...
    async def update_conversation(self, conversation_id: int, **fields: Any) -> None: ...
    async def reset_conversation(self, conversation_id: int) -> None:
        """Borra historial + slots y regresa la conversación a estado inicial
        (comando /reset de la línea de pruebas)."""
        ...

    # historial LLM
    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None: ...
    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]: ...

    # slots ofrecidos
    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None: ...
    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]: ...
    async def clear_offered_slots(self, conversation_id: int) -> None: ...

    # cola de envíos pendientes (respuestas que no pudieron salir en el turno)
    async def enqueue_pending_send(
        self,
        conversation_id: int,
        crm_conversation_id: str,
        content: str,
        organization_id: str = "",
        organization_slug: str = "",
        dispatch_id: str = "",
    ) -> int: ...
    async def due_pending_sends(self, now: datetime) -> list[PendingSend]: ...
    async def mark_pending_send_delivered(self, pending_id: int) -> None: ...
    async def mark_pending_send_abandoned(self, pending_id: int) -> None: ...
    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None: ...

    # seguimiento
    async def due_followups(self, now: datetime) -> list[Conversation]: ...
    async def claim_followup(self, conversation_id: int) -> bool:
        """Marca followup_sent=True atómicamente. True si ESTA llamada lo ganó."""
        ...

    async def ping(self) -> None: ...
    async def aclose(self) -> None: ...


# ------------------------------------------------------- fake en memoria ---


class MemoryStore:
    """Implementación en memoria del Store — solo para tests."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.processed: set[str] = set()
        self.relays: dict[int, RelayItem] = {}
        self.conversations: dict[int, Conversation] = {}
        self._conv_by_identity: dict[tuple[str, str], int] = {}
        self.messages: list[BotMessage] = []
        self.offered: dict[int, list[OfferedSlot]] = {}
        self.pending_sends: dict[int, PendingSend] = {}

    async def mark_processed(self, wa_message_id: str) -> bool:
        if wa_message_id in self.processed:
            return False
        self.processed.add(wa_message_id)
        return True

    async def enqueue_relay(self, body: bytes, signature: str | None) -> int:
        rid = next(self._ids)
        now = utcnow()
        self.relays[rid] = RelayItem(
            id=rid, body=body, signature=signature, attempts=0,
            created_at=now, next_retry_at=now,
        )
        return rid

    async def due_relays(self, now: datetime) -> list[RelayItem]:
        return [
            r for r in sorted(self.relays.values(), key=lambda r: r.id)
            if r.delivered_at is None and r.abandoned_at is None
            and r.next_retry_at <= now
        ]

    async def mark_relay_delivered(self, relay_id: int) -> None:
        self.relays[relay_id].delivered_at = utcnow()

    async def mark_relay_abandoned(self, relay_id: int) -> None:
        self.relays[relay_id].abandoned_at = utcnow()

    async def reschedule_relay(
        self, relay_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        item = self.relays[relay_id]
        item.attempts = attempts
        item.next_retry_at = next_retry_at

    async def get_or_create_conversation(
        self,
        wa_identity: str,
        organization_id: str = "",
        organization_slug: str = "",
    ) -> Conversation:
        clave = (organization_id, wa_identity)
        cid = self._conv_by_identity.get(clave)
        if cid is not None:
            return self.conversations[cid]
        cid = next(self._ids)
        conv = Conversation(
            id=cid,
            wa_identity=wa_identity,
            organization_id=organization_id,
            organization_slug=organization_slug,
        )
        self.conversations[cid] = conv
        self._conv_by_identity[clave] = cid
        return conv

    async def update_conversation(self, conversation_id: int, **fields: Any) -> None:
        conv = self.conversations[conversation_id]
        for key, value in fields.items():
            setattr(conv, key, value)

    async def reset_conversation(self, conversation_id: int) -> None:
        self.messages = [
            m for m in self.messages if m.conversation_id != conversation_id
        ]
        self.offered.pop(conversation_id, None)
        conv = self.conversations[conversation_id]
        conv.phase = "descubrimiento"
        conv.greeted = False
        conv.media_notice_sent = False
        conv.followup_due_at = None
        conv.followup_sent = False
        conv.stalled_at = None

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None:
        self.messages.append(
            BotMessage(
                id=next(self._ids),
                conversation_id=conversation_id,
                role=role,
                content=content,
                wa_message_id=wa_message_id,
            )
        )

    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]:
        msgs = [m for m in self.messages if m.conversation_id == conversation_id]
        return msgs[-limit:]

    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None:
        self.offered[conversation_id] = list(slots)

    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]:
        return list(self.offered.get(conversation_id, []))

    async def clear_offered_slots(self, conversation_id: int) -> None:
        self.offered.pop(conversation_id, None)

    async def enqueue_pending_send(
        self,
        conversation_id: int,
        crm_conversation_id: str,
        content: str,
        organization_id: str = "",
        organization_slug: str = "",
        dispatch_id: str = "",
    ) -> int:
        pid = next(self._ids)
        now = utcnow()
        self.pending_sends[pid] = PendingSend(
            id=pid, conversation_id=conversation_id,
            crm_conversation_id=crm_conversation_id, content=content,
            attempts=0, created_at=now, next_retry_at=now,
            organization_id=organization_id,
            organization_slug=organization_slug,
            dispatch_id=dispatch_id,
        )
        return pid

    async def due_pending_sends(self, now: datetime) -> list[PendingSend]:
        return [
            p for p in sorted(self.pending_sends.values(), key=lambda p: p.id)
            if p.delivered_at is None and p.abandoned_at is None
            and p.next_retry_at <= now
        ]

    async def mark_pending_send_delivered(self, pending_id: int) -> None:
        self.pending_sends[pending_id].delivered_at = utcnow()

    async def mark_pending_send_abandoned(self, pending_id: int) -> None:
        self.pending_sends[pending_id].abandoned_at = utcnow()

    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        item = self.pending_sends[pending_id]
        item.attempts = attempts
        item.next_retry_at = next_retry_at

    async def due_followups(self, now: datetime) -> list[Conversation]:
        return [
            c for c in self.conversations.values()
            if c.followup_due_at is not None
            and c.followup_due_at <= now
            and not c.followup_sent
            and c.phase != "cerrada"
        ]

    async def claim_followup(self, conversation_id: int) -> bool:
        conv = self.conversations[conversation_id]
        if conv.followup_sent:
            return False
        conv.followup_sent = True
        return True

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------- contexto ---


@dataclass
class AppContext:
    """Dependencias vivas de la app — inyectables en tests."""

    settings: "Settings"
    store: Store
    crm: Any  # CrmClient
    llm: Any  # OpenAiLlm o fake con .complete()
    profile: Any | None = None  # ProfileProvider; None en tests = perfil mínimo
    # Modo multi-organización: el registro de clientes por organización.
    # None fuera de ese modo — una Nea de un solo negocio no lo necesita, y
    # que sea None es lo que hace evidente en qué modo está corriendo.
    registro: Any | None = None  # RegistroDeOrganizaciones
    # (organization_id, slug) del turno en curso. La pone el despacho al armar
    # el contexto del turno; None fuera del modo multi-organización.
    organizacion: tuple[str, str] | None = None
    coalescer: Any | None = None
    relay_wake: asyncio.Event = field(default_factory=asyncio.Event)
    # ¿El CRM de esta instancia tiene motor de agenda? Vocero lo trae detrás de
    # una bandera de despliegue y viene apagado por defecto. Se resuelve al
    # arrancar (y se corrige solo si en caliente resulta que no está), para no
    # ofrecerle horarios a un lead contra un CRM que no puede agendarlos.
    agenda_enabled: bool = True
    # Un candado por identidad: los turnos de UNA conversación se serializan.
    # Sin esto, una ráfaga que llega mientras el turno anterior sigue en vuelo
    # abre un segundo turno con contexto viejo (se reservó una cita antes de
    # leer el mensaje que la corregía, y salieron dos respuestas encimadas).
    turn_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Cuántos turnos tienen tomado (o esperan) el candado de esa identidad.
    turn_lock_users: dict[str, int] = field(default_factory=dict)

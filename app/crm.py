"""Cliente httpx del API de servicio del CRM (bot gateway de vocero-crm).

Endpoints:
  GET  /api/bot/profile                               → agent profile + KB (404 = sin perfil)
  GET  /api/bot/context?waIdentity=...
  POST /api/bot/messages   {conversationId, text}   → 409 ai_paused|window_closed
  PUT  /api/bot/ficha      {conversationId, ficha}
  POST /api/bot/handoff    {conversationId, reason}
  GET  /api/bot/availability?conversationId=&limit=&perDay=&days=
                                                    → huecos repartidos por día,
                                                      REGISTRADOS como la oferta
                                                      de esa conversación
  POST /api/bot/bookings   {conversationId, startUtc} → 409 slot_taken + slots frescos
  PATCH /api/bot/bookings  {conversationId, startUtc} → mueve la próxima cita
  GET  /api/bot/media/{mediaId}                       → binario + content-type
  POST /api/bot/reset      {conversationId}           → reinicio de pruebas (002)
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("nea.crm")


class CrmError(Exception):
    """Fallo genérico hablando con el CRM (red, 5xx, 401...)."""


class CrmConflict(CrmError):
    """409 tipado del CRM: ai_paused | window_closed | slot_taken."""

    def __init__(self, code: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.payload = payload or {}


class AgendaUnavailable(CrmError):
    """El CRM no tiene agenda (404 en `/api/bot/availability|bookings`).

    Vocero trae el motor de agendamiento detrás de una bandera de despliegue
    (`AGENDA`) y viene APAGADA por defecto: en una instancia así esos endpoints
    no existen. No es una caída ni un error de configuración de Nea — es una
    capacidad que ese CRM no ofrece, y el agente tiene que dejar de prometer
    citas en vez de reintentar contra una puerta que no está.
    """


class ConflictWithSlots(CrmConflict):
    """409 de agenda que viene con horarios para volver a ofrecer."""

    def __init__(self, code: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(code, payload)
        self.slots: list[dict[str, Any]] = list((payload or {}).get("slots") or [])


class SlotTaken(ConflictWithSlots):
    """El slot se ocupó entre oferta y confirmación; trae alternativas frescas."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        super().__init__("slot_taken", payload)


class SlotNotOffered(ConflictWithSlots):
    """El CRM no reconoce ese horario como ofrecido A ESTA conversación.

    Desde que Vocero bajó la garantía "solo se reserva lo que se ofreció" a su
    propio núcleo, la lista que manda aquí es la AUTORIDAD: si no coincide con
    la que tiene Nea, la de Nea está vieja y hay que resincronizar con esta.
    """

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        super().__init__("slot_not_offered", payload)


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _conflict_code(response: httpx.Response) -> str:
    """Código tipado de un 409.

    El CRM anida `{"error": {"code": ...}}`; la forma plana `{"code": ...}`
    solo vivía en los mocks, así que en producción TODO 409 se leía como
    "conflict" genérico y el camino de `slot_taken` (re-ofrecer alternativas
    frescas) nunca se activaba. Se toleran las dos formas.
    """
    payload = _payload(response)
    nested = payload.get("error")
    if isinstance(nested, dict) and nested.get("code"):
        return str(nested["code"])
    return str(payload.get("code") or "conflict")


def _booking_conflict(response: httpx.Response) -> CrmConflict:
    """409 de agenda: `slot_taken` viene con alternativas frescas adjuntas."""
    payload = _payload(response)
    code = _conflict_code(response)
    if code == "slot_taken":
        return SlotTaken(payload)
    if code == "slot_not_offered":
        return SlotNotOffered(payload)
    return CrmConflict(code, payload)


# Catálogo cerrado del CRM para handoff.reason (006). El LLM escribe motivos
# libres ("pidió humano", "duda técnica") — aquí se normalizan SIEMPRE:
# certificación 002 cazó en vivo que un reason fuera de catálogo era 422 y el
# handoff se perdía con la IA aún activa.
HANDOFF_REASONS = frozenset({"cliente", "modelo", "error", "ventana", "hostilidad"})


def canonical_handoff_reason(reason: str | None) -> str:
    r = (reason or "").strip().lower()
    if r in HANDOFF_REASONS:
        return r
    if "hostil" in r or "groser" in r or "insult" in r:
        return "hostilidad"
    if any(k in r for k in ("humano", "persona", "pidi", "lead_request", "hablar")):
        return "cliente"
    if "error" in r:
        return "error"
    return "modelo"


class CrmClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CrmError(f"error de red hacia el CRM: {exc}") from exc

    async def get_context(self, wa_identity: str) -> dict[str, Any] | None:
        """Contexto conversacional; None si el CRM aún no conoce la identidad (404)."""
        resp = await self._request(
            "GET", "/api/bot/context", params={"waIdentity": wa_identity}
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmError(f"context devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def get_profile(self) -> dict[str, Any] | None:
        """Agent profile + knowledge base del negocio; None si el CRM no lo
        expone todavía (404) — el bot cae al brief local (app/profile.py)."""
        resp = await self._request("GET", "/api/bot/profile")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmError(f"profile devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/api/bot/messages",
            json={"conversationId": conversation_id, "text": text},
        )
        if resp.status_code == 409:
            raise CrmConflict(_conflict_code(resp))
        if resp.status_code != 200:
            raise CrmError(f"messages devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def put_ficha(
        self, conversation_id: str, ficha: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._request(
            "PUT",
            "/api/bot/ficha",
            json={"conversationId": conversation_id, "ficha": ficha},
        )
        if resp.status_code != 200:
            raise CrmError(f"ficha devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def post_handoff(self, conversation_id: str, reason: str | None = None) -> None:
        resp = await self._request(
            "POST",
            "/api/bot/handoff",
            json={
                "conversationId": conversation_id,
                "reason": canonical_handoff_reason(reason),
            },
        )
        if resp.status_code != 200:
            raise CrmError(f"handoff devolvió {resp.status_code}")

    async def get_availability(
        self,
        conversation_id: str,
        limit: int = 6,
        per_day: int | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Huecos libres, y a la vez la OFERTA de esta conversación.

        `conversationId` no es opcional: el CRM guarda contra esa conversación
        exactamente lo que devuelve aquí, y después solo acepta reservar uno de
        esos instantes. Pedir disponibilidad sin decir para quién es responde
        422 — y esa era la causa de que Nea no pudiera agendar contra un Vocero
        reciente.

        Con `per_day` el CRM reparte los huecos entre días distintos (si no,
        los primeros N se los come el día de hoy) y los devuelve con el día
        desambiguado en `dayLabel`.
        """
        params: dict[str, Any] = {
            "conversationId": conversation_id,
            "limit": limit,
        }
        if per_day:
            params["perDay"] = per_day
        if days:
            params["days"] = days
        resp = await self._request("GET", "/api/bot/availability", params=params)
        if resp.status_code == 404:
            raise AgendaUnavailable("este CRM no tiene el motor de agenda encendido")
        if resp.status_code != 200:
            raise CrmError(f"availability devolvió {resp.status_code}")
        slots = resp.json().get("slots") or []
        return list(slots)

    async def agenda_available(self) -> bool:
        """¿Este CRM ofrece agenda?

        Se pregunta SIN `conversationId` a propósito: con la agenda encendida
        el CRM responde 422 ("falta conversationId") y con ella apagada, 404.
        Basta para distinguir, no ensucia la oferta de ninguna conversación y
        no necesita un endpoint nuevo del CRM.

        Ante cualquier otra cosa (red caída, 5xx) se asume que SÍ hay agenda:
        equivocarse hacia "sí" solo cuesta un intento fallido más adelante,
        que ya degrada solo; equivocarse hacia "no" apagaría el agendamiento
        de una instancia que sí lo tiene.
        """
        try:
            resp = await self._request("GET", "/api/bot/availability")
        except CrmError:
            return True
        return resp.status_code != 404

    async def create_booking(
        self, conversation_id: str, start_utc: str
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/api/bot/bookings",
            json={"conversationId": conversation_id, "startUtc": start_utc},
        )
        if resp.status_code == 409:
            raise _booking_conflict(resp)
        if resp.status_code == 404:
            raise AgendaUnavailable("este CRM no tiene el motor de agenda encendido")
        # El CRM real responde 201 Created (REST); los mocks viejos daban 200.
        if resp.status_code not in (200, 201):
            raise CrmError(f"bookings devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def reschedule_booking(
        self, conversation_id: str, start_utc: str
    ) -> dict[str, Any]:
        """Mueve la PRÓXIMA cita activa del lead a otro horario ofrecido.

        Antes esto no existía y el agente tenía que hacer handoff: la IA se
        pausaba y el lead quedaba sin nadie del otro lado.
        """
        resp = await self._request(
            "PATCH",
            "/api/bot/bookings",
            json={"conversationId": conversation_id, "startUtc": start_utc},
        )
        if resp.status_code == 409:
            raise _booking_conflict(resp)
        if resp.status_code == 404:
            # Ojo con este 404: puede ser "no hay cita que mover" (agenda
            # encendida) o "aquí no hay agenda". Los distingue el cuerpo: el
            # primero trae el sobre de error del CRM; el segundo viene vacío.
            if _payload(resp):
                raise CrmConflict("no_booking", _payload(resp))
            raise AgendaUnavailable("este CRM no tiene el motor de agenda encendido")
        if resp.status_code != 200:
            raise CrmError(f"reschedule devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def post_typing(self, conversation_id: str) -> None:
        """Marca leído + "escribiendo…" (007). Best-effort: sin reintentos."""
        resp = await self._request(
            "POST",
            "/api/bot/typing",
            json={"conversationId": conversation_id},
            timeout=8.0,
        )
        if resp.status_code != 200:
            raise CrmError(f"typing devolvió {resp.status_code}")

    async def post_reset(self, conversation_id: str) -> None:
        """Reinicio de pruebas (spec 002): ficha limpia + IA reactivada + etapa
        al inicio en el CRM. Solo lo dispara el comando /reset de la allowlist."""
        resp = await self._request(
            "POST", "/api/bot/reset", json={"conversationId": conversation_id}
        )
        if resp.status_code != 200:
            raise CrmError(f"reset devolvió {resp.status_code}")

    async def get_media(self, media_id: str) -> tuple[bytes, str]:
        """Descarga un binario de Meta A TRAVÉS del CRM (el token vive allá).

        Devuelve (bytes, mime). Timeout amplio: los adjuntos pueden pesar.
        """
        resp = await self._request(
            "GET", f"/api/bot/media/{media_id}", timeout=60.0
        )
        if resp.status_code != 200:
            raise CrmError(f"media devolvió {resp.status_code}")
        mime = resp.headers.get("content-type") or "application/octet-stream"
        return resp.content, mime

    async def aclose(self) -> None:
        await self._http.aclose()

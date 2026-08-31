"""Cliente del CRM para el modo cloud: la superficie `/api/brains/*`.

Vocero tiene dos superficies para un agente externo, y no son la misma:

- `/api/bot/*`   — el Vocero de un solo negocio. Autentica con `X-API-Key` y
                   la organización es "la única que hay".
- `/api/brains/*` — el Vocero multitenant. Autentica con el secreto de UNA
                   organización, y el CRM verifica que el secreto y el slug
                   correspondan: un cerebro no puede actuar sobre otra
                   organización aunque adivine su nombre.

Esta clase habla la segunda. Hereda de `CrmClient` a propósito: los métodos que
solo cambian de ruta se reescriben en `_request` con una tabla, y aquí abajo
quedan únicamente aquellos cuya RESPUESTA tiene otra forma. Así, cuando el
contrato crezca, se ve de un vistazo qué difiere de verdad.

Nada de esto se activa sin `VOCERO_MODE=cloud`. Una instalación existente no
cambia por que este archivo exista.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.crm import (
    AgendaUnavailable,
    CrmClient,
    CrmError,
    _booking_conflict,
)

logger = logging.getLogger("nea.crm.brains")

# Las rutas que solo cambian de sitio. Las que además cambian de forma tienen
# su propio método más abajo.
RUTAS = {
    "/api/bot/context": "/api/brains/context",
    "/api/bot/messages": "/api/brains/messages",
    "/api/bot/ficha": "/api/brains/ficha",
    "/api/bot/handoff": "/api/brains/handoff",
    "/api/bot/typing": "/api/brains/typing",
    "/api/bot/availability": "/api/brains/agenda/slots",
    "/api/bot/bookings": "/api/brains/agenda/book",
}


class BrainsCrmClient(CrmClient):
    def __init__(
        self,
        base_url: str,
        secret: str,
        organization: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Vocero-Organization": organization,
            },
            timeout=timeout,
        )
        # De qué conversación es cada identidad. Lo llena el despacho: el CRM
        # ya sabe de quién es el mensaje, así que Nea no tiene que averiguarlo.
        self._conversaciones: dict[str, str] = {}
        # El perfil viaja DENTRO del contexto en esta superficie. Se guarda al
        # pedirlo para que `get_profile()` no gaste una llamada de más por
        # turno — y para que el perfil sea el de la conversación que se está
        # atendiendo, no el de una petición suelta.
        self._perfil: dict[str, Any] | None = None
        # Las credenciales de IA del MIEMBRO, tal como vinieron en el
        # ultimo contexto. Solo llegan si el CRM considera de confianza a
        # este despliegue; si no, se queda en None y el turno no corre.
        self._llm: dict[str, Any] | None = None
        # Contexto ya traido para una conversacion, pendiente de consumir.
        # Lo llena `precargar` desde el despacho para que el turno no lo
        # vuelva a pedir: una llamada HTTP por despacho, no dos.
        self._precargado: dict[str, dict[str, Any]] = {}

    def registrar(self, identity: str, conversation_id: str) -> None:
        """Asocia una identidad con su conversación, desde el despacho."""
        self._conversaciones[identity] = conversation_id

    async def precargar(self, conversation_id: str) -> dict[str, Any] | None:
        """Trae el contexto AHORA y lo deja listo para el turno.

        Existe por las credenciales de IA: el turno necesita saber con que
        llave va a pensar ANTES de empezar a pensar, y esa llave viene en el
        contexto. Sin esto habria que pedir el contexto dos veces por
        despacho — una para las credenciales y otra dentro del turno.
        """
        resp = await self._request(
            "GET", "/api/bot/context", params={"conversationId": conversation_id}
        )
        if resp.status_code != 200:
            logger.warning(
                "precarga del contexto de %s: el CRM respondio %s",
                conversation_id,
                resp.status_code,
            )
            return None
        data: dict[str, Any] = resp.json()
        self._perfil = _perfil_desde_contexto(data)
        self._llm = data.get("llm") or None
        self._precargado[conversation_id] = data
        return data

    def credenciales_llm(self) -> dict[str, Any] | None:
        """Las del ultimo contexto. None = el CRM no las entrego."""
        return self._llm

    def _request(self, method: str, url: str, **kwargs: Any):  # type: ignore[override]
        return super()._request(method, RUTAS.get(url, url), **kwargs)

    async def get_context(self, wa_identity: str) -> dict[str, Any] | None:
        """El contexto, pedido por conversación y no por identidad.

        `/api/brains/context` no acepta `waIdentity`: el ámbito de un cerebro
        es la organización de su secreto, y dejar buscar por teléfono sería
        darle una forma de preguntar por gente que no le corresponde.

        Sin conversación conocida devuelve None, que es lo mismo que dice el
        camino de siempre cuando el CRM aún no conoce la identidad: el turno se
        queda callado en vez de inventar.
        """
        conversation_id = self._conversaciones.get(wa_identity)
        if not conversation_id:
            logger.warning(
                "identidad %s sin conversación conocida — ¿llegó por el despacho?",
                wa_identity,
            )
            return None

        # Se consume UNA vez: asi el turno usa lo que ya se trajo en el
        # despacho, pero un segundo turno de la misma conversacion vuelve a
        # preguntar en vez de contestar con un contexto viejo.
        precargado = self._precargado.pop(conversation_id, None)
        if precargado is not None:
            self._perfil = _perfil_desde_contexto(precargado)
            return precargado

        resp = await self._request(
            "GET", "/api/bot/context", params={"conversationId": conversation_id}
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmError(f"context devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        self._perfil = _perfil_desde_contexto(data)
        self._llm = data.get("llm") or self._llm
        return data

    async def get_profile(self) -> dict[str, Any] | None:
        """El perfil que vino con el último contexto.

        En esta superficie el perfil no tiene endpoint propio: viaja con el
        contexto de la conversación. Devolver el guardado —en vez de pedirlo
        otra vez— evita una llamada por turno y garantiza que el perfil es el
        de la organización que se está atendiendo.
        """
        return self._perfil

    async def agenda_available(self) -> bool:
        """¿Este CRM agenda? 404 = la bandera está apagada allá."""
        try:
            resp = await self._request(
                "GET", "/api/bot/availability", params={"conversationId": "cv_sonda"}
            )
        except CrmError:
            # Sin respuesta no se puede concluir que NO haya agenda. Se asume
            # que sí y el primer intento real lo dirá: prometer menos de lo que
            # hay es tan malo como prometer de más.
            return True
        return resp.status_code != 404

    async def get_availability(
        self,
        conversation_id: str,
        limit: int = 6,
        per_day: int | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Huecos libres, y a la vez la OFERTA de esta conversación.

        `limit`, `perDay` y `days` no viajan: en esta superficie el reparto lo
        decide el CRM, que es quien conoce el horario del negocio. Se aceptan
        en la firma para no romper a quien ya llama con ellos.
        """
        resp = await self._request(
            "GET",
            "/api/bot/availability",
            params={"conversationId": conversation_id},
        )
        if resp.status_code == 404:
            raise AgendaUnavailable("este CRM no tiene el motor de agenda encendido")
        if resp.status_code != 200:
            raise CrmError(f"availability devolvió {resp.status_code}")
        slots = resp.json().get("slots") or []
        return list(slots)[:limit]

    async def create_booking(
        self, conversation_id: str, start_utc: str
    ) -> dict[str, Any]:
        return await self._agendar("POST", conversation_id, start_utc, "bookings")

    async def reschedule_booking(
        self, conversation_id: str, start_utc: str
    ) -> dict[str, Any]:
        return await self._agendar("PATCH", conversation_id, start_utc, "reschedule")

    async def _agendar(
        self, method: str, conversation_id: str, start_utc: str, que: str
    ) -> dict[str, Any]:
        resp = await self._request(
            method,
            "/api/bot/bookings",
            json={"conversationId": conversation_id, "startUtc": start_utc},
        )
        if resp.status_code == 409:
            raise _booking_conflict(resp)
        if resp.status_code == 404:
            raise AgendaUnavailable("este CRM no tiene el motor de agenda encendido")
        if resp.status_code not in (200, 201):
            raise CrmError(f"{que} devolvió {resp.status_code}")
        return _aplanar_reserva(resp.json())

    async def post_reset(self, conversation_id: str) -> None:
        """El reinicio de pruebas no existe en esta superficie.

        No es un fallo: es una herramienta del entorno de un solo negocio. Se
        registra y se sigue, porque hacerlo estallar convertiría un comando de
        pruebas en una caída del turno.
        """
        logger.info("reset no disponible en modo cloud — ignorado")


def _perfil_desde_contexto(data: dict[str, Any]) -> dict[str, Any]:
    """Traduce `agent` + `knowledge` a lo que espera `profile_from_payload`.

    El conocimiento llega como pares pregunta/respuesta y se aplana a texto:
    es lo que ya sabe consumir el prompt, y hacerlo aquí evita tocarlo.
    """
    agent = data.get("agent") or {}
    knowledge = data.get("knowledge") or []
    bloques = [
        f"P: {k.get('question')}\nR: {k.get('answer')}"
        for k in knowledge
        if isinstance(k, dict) and k.get("question") and k.get("answer")
    ]
    return {
        "profile": agent,
        "kb": "\n\n".join(bloques) if bloques else None,
        # Esta superficie todavía no expone los recursos del negocio (enlaces
        # que el agente puede compartir). Lista vacía en vez de omitirla: el
        # consumidor ya la trata como opcional.
        "resources": [],
    }


def _aplanar_reserva(payload: dict[str, Any]) -> dict[str, Any]:
    """`{ok, booking:{…}}` → los campos al ras, como los devuelve `/api/bot/*`.

    Se aplana aquí y no en quien consume para que el resto de Nea no tenga que
    saber por qué superficie entró la respuesta.
    """
    booking = payload.get("booking")
    if not isinstance(booking, dict):
        return payload
    return {**payload, **booking}

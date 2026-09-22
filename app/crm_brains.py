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

import hashlib
import logging
from typing import Any

import httpx

from app.crm import (
    CrmClient,
    CrmConflict,
    CrmError,
    _404_de_agenda,
    _agenda_apagada,
    _booking_conflict,
    _conflict_code,
    _payload,
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

# Las rutas con un identificador al final no pueden vivir en ``RUTAS``: el
# valor concreto (por ejemplo el id de Meta) cambia en cada petición. Mantener
# el prefijo explícito evita que el cliente cloud herede accidentalmente la
# superficie ``/api/bot`` y sea rechazado antes de descargar el adjunto.
RUTAS_POR_PREFIJO = {
    "/api/bot/media/": "/api/brains/media/",
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
        # Y qué despacho se está contestando en cada conversación: el CRM lo
        # exige al responder, para no mandar dos veces lo mismo si reintenta.
        self._despachos: dict[str, str] = {}
        self._envelopes: dict[str, dict[str, Any]] = {}
        self.supports_agenda_v2 = False
        # El perfil viaja DENTRO del contexto en esta superficie. Se guarda al
        # pedirlo para que `get_profile()` no gaste una llamada de más por
        # turno — y para que el perfil sea el de la conversación que se está
        # atendiendo, no el de una petición suelta.
        self._perfil: dict[str, Any] | None = None
        # Como puede pensar esta organizacion: la ruta por la que el CRM
        # piensa por Nea, y que modelos acepta. NO trae ninguna llave — esa
        # se queda en el CRM. Solo llega si el CRM considera de confianza a
        # este despliegue; si no, se queda en None y el turno no corre.
        self._llm: dict[str, Any] | None = None
        # Contexto ya traido para una conversacion, pendiente de consumir.
        # Lo llena `precargar` desde el despacho para que el turno no lo
        # vuelva a pedir: una llamada HTTP por despacho, no dos.
        self._precargado: dict[str, dict[str, Any]] = {}

    def registrar(self, identity: str, conversation_id: str) -> None:
        """Asocia una identidad con su conversación, desde el despacho."""
        self._conversaciones[identity] = conversation_id

    def registrar_despacho(self, conversation_id: str, dispatch_id: str) -> None:
        """Asocia una conversación con el despacho que se está contestando.

        `POST /api/brains/messages` EXIGE el `dispatchId` (contrato §2.1): es
        lo que le deja al CRM ser idempotente, porque reintenta los despachos
        y sin él respondería dos veces al mismo cliente final.

        Se guarda aquí y no se pasa por parámetro porque el turno no sabe de
        despachos —ni tiene por qué—: recibe una conversación y un texto. Es la
        misma razón por la que `registrar` existe.
        """
        if dispatch_id:
            self._despachos[conversation_id] = dispatch_id

    def registrar_envelope(self, conversation_id: str, payload: dict[str, Any]) -> None:
        self.supports_agenda_v2 = "agenda_v2" in payload.get("capabilities", [])
        if self.supports_agenda_v2:
            self._envelopes[conversation_id] = {"dispatchId": payload.get("dispatchId"), "brainGeneration": payload.get("brainGeneration")}

    async def set_coordination_consent(self, conversation_id: str, consent: bool) -> dict[str, Any]:
        response = await self._request("POST", "/api/brains/agenda/followup", json={"conversationId": conversation_id, "consent": consent, **self._envelopes.get(conversation_id, {})})
        if not response.is_success:
            raise CrmConflict("followup_not_allowed")
        return response.json()

    async def list_bookings(self, conversation_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/brains/agenda/bookings", params={"conversationId": conversation_id})
        if not response.is_success:
            raise CrmError("No se pudieron consultar las citas")
        return list(response.json().get("bookings") or [])

    async def cancel_booking(self, conversation_id: str, selection_token: str, confirmation: bool) -> dict[str, Any]:
        body = self._operation(conversation_id, "cancel", selection_token)
        body.update(selectionToken=selection_token, confirmation=confirmation)
        response = await self._request("POST", "/api/brains/agenda/cancel", json=body)
        if response.status_code == 409:
            raise _booking_conflict(response)
        if not response.is_success:
            raise CrmError("No se pudo cancelar la cita")
        return response.json()

    def _operation(self, conversation_id: str, action: str, value: str) -> dict[str, Any]:
        envelope = self._envelopes.get(conversation_id, {})
        key = hashlib.sha256(f"{conversation_id}:{self.despacho_de(conversation_id)}:{action}:{value}".encode()).hexdigest()
        return {"conversationId": conversation_id, "idempotencyKey": key, **envelope}

    def despacho_de(self, conversation_id: str) -> str:
        """El despacho en curso de esa conversación, o cadena vacía."""
        return self._despachos.get(conversation_id, "")

    async def precargar(self, conversation_id: str) -> dict[str, Any] | None:
        """Trae el contexto AHORA y lo deja listo para el turno.

        Existe por las credenciales de IA: el turno necesita saber con que
        llave va a pensar ANTES de empezar a pensar, y esa llave viene en el
        contexto. Sin esto habria que pedir el contexto dos veces por
        despacho — una para las credenciales y otra dentro del turno.
        """
        resp = await self._request(
            "GET", "/api/bot/context", params={"conversationId": conversation_id, "contextVersion": "2"}
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
        self.supports_coordination = bool((data.get("agent") or {}).get("coordinationFollowupEnabled"))
        self._llm = data.get("llm") or None
        self._precargado[conversation_id] = data
        return data

    def credenciales_llm(self) -> dict[str, Any] | None:
        """La configuracion de IA del ultimo contexto.

        None = el CRM no ofrece pensar por esta organizacion (este despliegue
        no es de confianza, o el miembro no tiene token activo). Las dos se
        tratan igual: sin turno, y la conversacion a un humano.
        """
        return self._llm

    def _request(self, method: str, url: str, **kwargs: Any):  # type: ignore[override]
        ruta = RUTAS.get(url, url)
        if ruta == url:
            for origen, destino in RUTAS_POR_PREFIJO.items():
                if url.startswith(origen):
                    ruta = destino + url.removeprefix(origen)
                    break
        return super()._request(method, ruta, **kwargs)

    async def send_message(
        self, conversation_id: str, text: str, dispatch_id: str = ""
    ) -> dict[str, Any]:
        """Responder por el cerebro. Dos cosas que el de `/api/bot` no hacía.

        **Manda el `dispatchId`.** El de siempre solo manda conversación y
        texto, así que el CRM rechazaba TODA respuesta con `422 invalid_body`.
        Nea pensaba bien, pagaba el modelo, y el texto no salía nunca.

        **Acepta cualquier 2xx.** Esta ruta responde `201` al crear, y el
        contrato lo avisa con todas las letras: «no compares contra 200 (esa
        comparación ya rompió toda la reserva de citas en producción una vez)».
        El cliente hacía exactamente eso: aun con el body correcto habría
        tratado el envío bueno como un fallo, y lo habría reintentado hasta
        encolarlo — con el mensaje YA entregado al cliente final.
        """
        dispatch_id = dispatch_id or self.despacho_de(conversation_id)
        resp = await self._request(
            "POST",
            "/api/bot/messages",
            json={
                "dispatchId": dispatch_id,
                "conversationId": conversation_id,
                "text": text,
            },
        )
        if resp.status_code == 409:
            raise CrmConflict(_conflict_code(resp))
        # Ventana de 24 h cerrada. En `/api/bot` era un 409 y aquí es un 422
        # (contrato §2.1). Se traduce al mismo conflicto para que el turno
        # calle igual que siempre en vez de reintentar algo que no va a
        # cambiar: la ventana no se abre reintentando.
        if resp.status_code == 422 and _conflict_code(resp) == "fuera_de_ventana":
            raise CrmConflict("window_closed")
        if not resp.is_success:
            raise CrmError(f"messages devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

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
            self.supports_coordination = bool((precargado.get("agent") or {}).get("coordinationFollowupEnabled"))
            return precargado

        resp = await self._request(
            "GET", "/api/bot/context", params={"conversationId": conversation_id, "contextVersion": "2"}
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmError(f"context devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        self._perfil = _perfil_desde_contexto(data)
        self.supports_coordination = bool((data.get("agent") or {}).get("coordinationFollowupEnabled"))
        self._llm = data.get("llm") or None
        return data

    async def get_profile(self) -> dict[str, Any] | None:
        """El perfil que vino con el último contexto.

        En esta superficie el perfil no tiene endpoint propio: viaja con el
        contexto de la conversación. Devolver el guardado —en vez de pedirlo
        otra vez— evita una llamada por turno y garantiza que el perfil es el
        de la organización que se está atendiendo.
        """
        return self._perfil

    async def sondear_agenda(self, timeout: float | None = None) -> bool | None:
        """¿Este CRM agenda? Solo el 404 VACÍO es la bandera apagada.

        Esta superficie autentica y exige conversación, así que la sonda
        pregunta por una inventada (`cv_sonda`). Con la agenda encendida el CRM
        contesta 404 CON su sobre de error («conversación no encontrada»), o
        401: el endpoint existe. Cuando aquí se leía cualquier 404 como
        apagada, una Nea cloud de un solo negocio arrancaba SIEMPRE sin agenda.

        Sin respuesta (red, timeout, 5xx) no se puede concluir: None, y quien
        pregunta decide — `agenda_available` asume que sí, porque prometer
        menos de lo que hay es tan malo como prometer de más.
        """
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            resp = await self._request(
                "GET",
                "/api/bot/availability",
                params={"conversationId": "cv_sonda"},
                **extra,
            )
        except CrmError:
            return None
        if resp.status_code >= 500:
            return None
        return not _agenda_apagada(resp)

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
        _404_de_agenda(resp, "availability")
        if resp.status_code != 200:
            raise CrmError("availability_unknown: no afirmar que el calendario está libre; reintentar o derivar")
        slots = resp.json().get("slots") or []
        return list(slots)[:limit]

    async def consultar_huecos(
        self,
        conversation_id: str,
        date: str | None = None,
        limit: int = 12,
        per_day: int | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """Huecos + `query` (hasta dónde llega lo consultado), sin recortar.

        Sin recorte a propósito: con `date` el CRM registra como oferta TODAS
        las horas de ese día, y si Nea se quedara con menos, rechazaría por su
        cuenta una hora que el CRM sí ofreció. `limit`, `per_day` y `days` no
        viajan, igual que en `get_availability`: el reparto lo decide el CRM.
        """
        params: dict[str, Any] = {"conversationId": conversation_id}
        if date:
            params["date"] = date
        resp = await self._request("GET", "/api/bot/availability", params=params)
        _404_de_agenda(resp, "availability")
        if resp.status_code != 200:
            raise CrmError("availability_unknown: no afirmar que el calendario está libre; reintentar o derivar")
        data = resp.json()
        return {"slots": list(data.get("slots") or []), "query": data.get("query")}

    async def create_booking(
        self, conversation_id: str, start_utc: str, reminder_consent: bool = False
    ) -> dict[str, Any]:
        return await self._agendar("POST", conversation_id, start_utc, "bookings", reminder_consent=reminder_consent)

    async def reschedule_booking(
        self, conversation_id: str, start_utc: str, selection_token: str | None = None
    ) -> dict[str, Any]:
        return await self._agendar("PATCH", conversation_id, start_utc, "reschedule", selection_token)

    async def _agendar(
        self, method: str, conversation_id: str, start_utc: str, que: str, selection_token: str | None = None, reminder_consent: bool = False
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"conversationId": conversation_id, "startUtc": start_utc}
        if self.supports_agenda_v2:
            operation_value = start_utc + (selection_token or "") + (":reminders" if reminder_consent else "")
            body.update(self._operation(conversation_id, method, operation_value))
            if method == "POST":
                body["reminderConsent"] = reminder_consent
            if method == "PATCH":
                body.update(selectionToken=selection_token, confirmation=True)
        resp = await self._request(
            method,
            "/api/bot/bookings",
            json=body,
        )
        if resp.status_code == 409:
            raise _booking_conflict(resp)
        if resp.status_code == 404:
            # Mismo 404 ambiguo que en `/api/bot`: con cuerpo es "no hay cita
            # que mover" (o, al reservar, "no encuentro esa conversación");
            # vacío, "aquí no hay agenda". Leerlos igual apagaba el
            # agendamiento de toda la instancia la primera vez que alguien
            # quería mover una cita que ya había pasado.
            if method == "PATCH" and not _agenda_apagada(resp):
                raise CrmConflict("no_booking", _payload(resp))
            _404_de_agenda(resp, que)
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
    bloques = []
    for k in knowledge:
        if not isinstance(k, dict):
            continue
        if k.get("kind") == "block" and isinstance(k.get("content"), str):
            bloques.append(k["content"])
        elif k.get("kind", "qa") == "qa" and k.get("question") and k.get("answer"):
            bloques.append(f"P: {k['question']}\nR: {k['answer']}")
    return {
        "profile": {**agent, "cloud": True},
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

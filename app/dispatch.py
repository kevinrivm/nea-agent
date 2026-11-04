"""La entrada de Nea: el CRM le DESPACHA la conversación.

Desde la 2.0 es la única. Meta le manda el webhook al CRM, el CRM guarda el
mensaje, espera a que el cliente termine de escribir y le entrega a Nea la
ráfaga firmada. Igual contra Vocero raíz que contra Vocero Cloud; lo único que
cambia es con qué llave viene firmada (`Settings.dispatch_secret`) y por dónde
se contesta (`/api/bot/*` o `/api/brains/*`).

Eso quita tres cosas que Nea hacía por estar delante del CRM:

- **No hay relay.** El CRM ya tiene el mensaje.
- **No hay coalesce aquí.** El CRM ya esperó a que el cliente terminara de
  escribir y entrega la ráfaga junta. Volver a esperar en Nea le sumaría
  segundos de silencio a un cliente que ya esperó los del CRM.
- **No se parsea a Meta.** Llega un evento normalizado, igual para WhatsApp que
  para Instagram, Messenger, TikTok o lo que venga después.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.multiorg import organizacion_del_despacho
from app.http_limits import limited_body
from app.state import AppContext, InboundMessage
from app.turn import conversation_lock, handle_flush, marca_de_leido

logger = logging.getLogger("nea.dispatch")

router = APIRouter()

# El CRM eliminó la conversación y pide que se olvide. Llega por la MISMA ruta
# y con la MISMA firma que un despacho: es el único canal que el CRM tiene
# hacia un cerebro, y un segundo endpoint sería una segunda URL que registrar.
EVENTO_OLVIDO = "conversation.deleted"


def firma_valida(body: bytes, header: str | None, secret: str) -> bool:
    """HMAC-SHA256 sobre el cuerpo CRUDO, con prefijo `sha256=`.

    Sobre el cuerpo crudo y no sobre el JSON re-serializado: un espacio de
    diferencia y la firma no coincide nunca. Es el error clásico de esta clase
    de integración, y por eso se valida antes de parsear nada.

    Sin secreto configurado se RECHAZA: una Nea sin `CRM_BOT_API_KEY` (o sin
    `CRM_BRAIN_SECRET` en cloud) no acepta nada. La firma prueba que quien
    despacha conoce la credencial del CRM.
    """
    if not secret or not header:
        return False
    esperado = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    recibido = header[7:] if header.startswith("sha256=") else header
    return hmac.compare_digest(recibido, esperado)


def mensajes_del_despacho(payload: dict[str, Any]) -> list[InboundMessage]:
    """Traduce el evento del CRM a lo que ya sabe consumir el turno.

    Tolerante a propósito: un mensaje de un tipo que Nea todavía no maneja no
    puede tumbar la ráfaga entera — se ignora y los demás siguen.
    """
    contacto = payload.get("contact") or {}
    identity = str(contacto.get("identity") or "")
    if not identity:
        return []
    canal = (payload.get("conversation") or {}).get("channel")
    # Solo `true` literal: la llave falta en una conversación real.
    prueba = (payload.get("conversation") or {}).get("test") is True

    out: list[InboundMessage] = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        mime = m.get("mimeType")
        out.append(
            InboundMessage(
                wa_message_id=str(m.get("id")) if m.get("id") else None,
                identity=identity,
                type=str(m.get("type") or "text"),
                text=m.get("text"),
                profile_name=contacto.get("displayName"),
                media_id=m.get("mediaId"),
                media_mime=mime,
                media_caption=m.get("caption"),
                # 031 — Lo que antes salía del webhook de Meta y ahora trae el
                # CRM, solo cuando aplica.
                media_filename=m.get("fileName"),
                # El CRM no guarda la marca `voice` de Meta. Una nota de voz de
                # WhatsApp es siempre ogg/opus; un audio adjunto casi nunca.
                media_voice=isinstance(mime, str) and mime.startswith("audio/ogg"),
                location=_ubicacion(m.get("location")),
                contact_names=_nombres(m.get("contacts")),
                channel=str(canal) if canal else None,
                prueba=prueba,
            )
        )
    return out


def _ubicacion(valor: Any) -> dict[str, Any] | None:
    """La ubicación del despacho, o None si no trae coordenadas."""
    if not isinstance(valor, dict):
        return None
    if not all(isinstance(valor.get(k), (int, float)) for k in ("latitude", "longitude")):
        return None
    return {k: valor[k] for k in ("latitude", "longitude", "name", "address") if valor.get(k) is not None}


def _nombres(valor: Any) -> list[str]:
    """Los nombres de las tarjetas de contacto del despacho."""
    if not isinstance(valor, list):
        return []
    return [
        str(t["name"])
        for t in valor
        if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip()
    ]


@router.post("/vocero/dispatch")
async def recibir(request: Request) -> Any:
    """Confirma el ACK después de persistir; el worker procesa fuera de la ruta.

    Los reintentos se deduplican en PostgreSQL por organización y dispatchId.
    """
    ctx: AppContext = request.app.state.ctx
    body = await limited_body(request)

    if not firma_valida(
        body,
        request.headers.get("x-vocero-signature"),
        ctx.settings.dispatch_secret,
    ):
        logger.warning("despacho con firma inválida o ausente — 401")
        return JSONResponse({"error": "firma inválida"}, status_code=401)

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        # 4xx a propósito: el CRM NO reintenta un cuerpo que no entendemos, y
        # así la conversación cae a un humano en vez de dar tres vueltas.
        logger.warning("despacho ilegible — 400")
        return JSONResponse({"error": "cuerpo ilegible"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "cuerpo inesperado"}, status_code=400)

    if payload.get("type") == EVENTO_OLVIDO:
        return await _recibir_olvido(ctx, payload)

    try:
        valid = (isinstance(payload.get("dispatchId"), str) and 0 < len(payload["dispatchId"]) <= 128
            and isinstance(payload.get("conversation"), dict) and isinstance(payload["conversation"].get("id"), str)
            and 0 < len(payload["conversation"]["id"]) <= 128
            and isinstance(payload.get("contact"), dict) and isinstance(payload["contact"].get("identity"), str)
            and isinstance(payload.get("messages"), list) and 0 < len(payload["messages"]) <= 100
            and isinstance(payload.get("organization", {}), dict))
        if payload.get("expiresAt"):
            from datetime import datetime
            expiry = datetime.fromisoformat(payload["expiresAt"].replace("Z", "+00:00"))
            valid = valid and expiry.tzinfo is not None
    except (ValueError, TypeError, AttributeError):
        valid = False
    org = organizacion_del_despacho(payload) if valid else None
    if not valid or (ctx.settings.multi_org and org is None):
        return JSONResponse({"error": "despacho inválido"}, status_code=422)
    try:
        await ctx.store.enqueue_dispatch(org[0] if org else ctx.settings.crm_organization, payload)
    except Exception:
        logger.exception("no se pudo persistir el despacho")
        return JSONResponse({"error": "almacenamiento indisponible"}, status_code=503)
    ctx.dispatch_wake.set()
    return {"status": "ok"}


def _texto(valor: Any) -> bool:
    return isinstance(valor, str) and 0 < len(valor) <= 128


async def _recibir_olvido(ctx: AppContext, payload: dict[str, Any]) -> Any:
    """Acusa recibo del borrado en cuanto queda guardado; olvidar va después.

    Va a la misma cola que los despachos, y no directo a la base, por el orden:
    la cola se atiende de la más vieja a la más nueva dentro de cada
    organización, así que un turno de esa conversación que aún esperaba termina
    ANTES de olvidar, y un mensaje nuevo de la misma persona empieza DESPUÉS.
    Borrar aquí mismo dejaría a ese turno escribiendo historial de una
    conversación que ya no existe.
    """
    conversation = payload.get("conversation")
    contact = payload.get("contact")
    valid = (
        _texto(payload.get("eventId"))
        and isinstance(conversation, dict) and _texto(conversation.get("id"))
        and isinstance(contact, dict) and _texto(contact.get("identity"))
        and isinstance(payload.get("organization", {}), dict)
    )
    org = organizacion_del_despacho(payload) if valid else None
    if not valid or (ctx.settings.multi_org and org is None):
        return JSONResponse({"error": "evento inválido"}, status_code=422)
    # La cola se indexa por `dispatchId`. El prefijo impide que un evento de
    # borrado se confunda con —o se deduplique contra— un despacho de verdad.
    fila = {**payload, "dispatchId": f"olvido:{payload['eventId']}"}
    try:
        await ctx.store.enqueue_dispatch(org[0] if org else ctx.settings.crm_organization, fila)
    except Exception:
        logger.exception("no se pudo persistir el borrado")
        return JSONResponse({"error": "almacenamiento indisponible"}, status_code=503)
    ctx.dispatch_wake.set()
    return {"status": "ok"}


async def olvidar(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Borra lo que Nea recuerda de la conversación que el CRM eliminó.

    Idempotente: repetirlo no encuentra nada que borrar. Por eso, a diferencia
    de un turno, una ejecución interrumpida SÍ se repite al recuperarla.
    """
    conversation_id = str(payload["conversation"]["id"])
    identity = str(payload["contact"]["identity"])
    org = organizacion_del_despacho(payload)
    # La misma regla que al guardar: con una Nea de un solo negocio la
    # conversación no lleva organización, y la cola se indexa por la del entorno.
    org_id = org[0] if (ctx.settings.multi_org and org) else ""
    cola = org[0] if org else ctx.settings.crm_organization
    # Con el candado del turno: en cloud la cola ya los pone en fila, pero el
    # seguimiento y el reintento de envíos corren por fuera de ella.
    async with conversation_lock(ctx, identity):
        borrada = await ctx.store.forget_conversation(org_id, identity, conversation_id)
        await ctx.store.forget_dispatches(cola, conversation_id, str(payload["dispatchId"]))
    logger.info(
        "olvido de %s: %s",
        conversation_id,
        "memoria borrada" if borrada else "no había nada que borrar",
    )


async def _procesar(ctx: AppContext, payload: dict[str, Any], *, durable: bool = False) -> None:
    dispatch_id = str(payload.get("dispatchId") or "")
    conversation = payload.get("conversation") or {}
    conversation_id = str(conversation.get("id") or "")

    mensajes = mensajes_del_despacho(payload)
    if not mensajes or not conversation_id:
        logger.warning("despacho %s sin mensajes o sin conversación", dispatch_id)
        return
    identity = mensajes[0].identity

    # Dedup por despacho: el CRM reintenta el DESPACHO entero cuando no le
    # acusamos recibo. Marcar aquí sus mensajes uno por uno dejaría pasar el
    # segundo intento con la mitad de la ráfaga ya marcada.
    if dispatch_id and not durable:
        fresco = await ctx.store.mark_processed(f"dsp:{dispatch_id}")
        if not fresco:
            logger.info("dedup: despacho %s ya procesado — ignorado", dispatch_id)
            return

    # Y por mensaje, que es lo que pide el contrato: un mensaje puede venir en
    # DOS despachos distintos. El segundo trae la ráfaga completa, así que
    # repite lo que Nea ya leyó. Lo leído lo anota el turno al guardarlo en su
    # historial (`_anotar_leidos`); aquí solo se consulta.
    org = organizacion_del_despacho(payload) if ctx.settings.multi_org else None
    org_id = org[0] if org else ""
    leidos = await ctx.store.already_processed(
        [marca_de_leido(org_id, m.wa_message_id) for m in mensajes if m.wa_message_id]
    )
    if leidos:
        nuevos = [
            m
            for m in mensajes
            if not m.wa_message_id
            or marca_de_leido(org_id, m.wa_message_id) not in leidos
        ]
        logger.info(
            "dedup: despacho %s repite %d mensaje(s) que ya leí — quedan %d",
            dispatch_id,
            len(mensajes) - len(nuevos),
            len(nuevos),
        )
        if not nuevos:
            return
        mensajes = nuevos

    if ctx.settings.multi_org:
        ctx_turno = await _contexto_multiorg(ctx, payload, identity, conversation_id)
        if ctx_turno is None:
            return  # el motivo ya quedó en el log, y la conversación con humano
    else:
        # El CRM ya sabe de quién es esta conversación; el cliente lo recuerda
        # para pedir el contexto de ESA conversación y no adivinarla por
        # teléfono (en `/api/brains` ni se puede).
        registrar = getattr(ctx.crm, "registrar", None)
        if callable(registrar):
            registrar(identity, conversation_id)
        ctx_turno = ctx

    # Y de qué despacho es. Sin esto el CRM rechaza la respuesta con 422: el
    # `dispatchId` es lo que le deja no responder dos veces al mismo cliente
    # cuando reintenta. Va aquí, para los dos modos, porque los dos contestan
    # por la misma ruta.
    registrar_despacho = getattr(ctx_turno.crm, "registrar_despacho", None)
    if callable(registrar_despacho):
        registrar_despacho(conversation_id, dispatch_id)

    registrar_envelope = getattr(ctx_turno.crm, "registrar_envelope", None)
    if callable(registrar_envelope):
        registrar_envelope(conversation_id, payload)

    # Directo al turno, sin coalescer: el CRM ya agrupó la ráfaga. Con la
    # conversación que el CRM despachó, para que la red de seguridad del turno
    # sepa a quién pasársela si revienta antes de leer el contexto.
    await handle_flush(
        ctx_turno, identity, mensajes, crm_conversation_id=conversation_id,
        propagate_errors=durable,
    )


async def _contexto_multiorg(
    ctx: AppContext,
    payload: dict[str, Any],
    identity: str,
    conversation_id: str,
) -> AppContext | None:
    """El contexto de ESTE turno, con las credenciales de SU organización.

    Una Nea multi-organización no puede tener un CRM ni un modelo fijos: el
    cliente del CRM lleva la credencial de una organización concreta, y el
    modelo se paga con la llave de ese miembro. Los dos se arman aquí y viven
    lo que vive el turno.

    Devuelve None cuando el turno NO debe correr. Los motivos se tratan igual
    —silencio y la conversación en manos de un humano— porque desde el lado
    del cliente son lo mismo: nadie le contestó, y alguien tiene que hacerlo.
    """
    registro = ctx.registro
    if registro is None:
        logger.error("modo multi-organización sin registro: no puedo contestar")
        return None

    org = organizacion_del_despacho(payload)
    if org is None:
        logger.warning("despacho sin organización en el cuerpo — ¿un CRM viejo?")
        return None
    org_id, slug = org

    cliente = registro.cliente(org_id, slug)
    cliente.registrar(identity, conversation_id)

    # El contexto se trae AQUÍ y no dentro del turno porque de él salen las
    # credenciales con las que el turno va a pensar. El turno lo reutiliza:
    # `precargar` lo deja servido y `get_context` lo consume sin otra llamada.
    contexto = await cliente.precargar(conversation_id)
    if contexto is None:
        logger.warning(
            "%s: el CRM no dio contexto de %s — sin turno", slug, conversation_id
        )
        await _a_humano(cliente, conversation_id, "error")
        return None

    config_ia = cliente.credenciales_llm()
    if not config_ia:
        # El CRM no ofrece pensar por esta organización. O este despliegue no
        # es de confianza, o el miembro no tiene token activo. En los dos
        # casos hay que callarse: pensar con la llave del entorno le cobraría
        # a la cuenta del dueño de la plataforma el consumo de sus miembros,
        # que es justo lo que este modo existe para evitar.
        logger.warning(
            "%s: el CRM no ofrece IA para %s; token inactivo o "
            "despliegue no confiable — sin turno",
            slug,
            conversation_id,
        )
        await _a_humano(cliente, conversation_id, "sin_credencial")
        return None

    return replace(
        ctx,
        crm=cliente,
        llm=registro.llm(org_id, slug, config_ia, ctx.settings),
        profile=registro.perfil(org_id, slug),
        organizacion=(org_id, slug),
    )


async def _a_humano(cliente: Any, conversation_id: str, motivo: str) -> None:
    """Marca la conversación para un humano, sin que eso pueda tumbar nada.

    Si hasta el handoff falla ya solo queda el log: el mensaje del cliente está
    guardado en la bandeja del CRM desde antes de que Nea lo viera, así que no
    se pierde nada aunque esta llamada no llegue.

    El `motivo` es obligatorio y no tiene valor por defecto a propósito. Lo
    tuvo —"error"— y por eso el miembro sin token leía «Error del proveedor de
    IA» en su bandeja: un mensaje que manda a revisar OpenRouter cuando al
    proveedor no se le había llamado siquiera. Quien escala sabe por qué; el
    default lo único que hacía era dejarle no decirlo.
    """
    try:
        await cliente.post_handoff(conversation_id, motivo)
    except Exception:
        logger.exception("no pude marcar %s para humano", conversation_id)
        raise

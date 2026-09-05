"""Entrada del modo cloud: el CRM le DESPACHA la conversación a Nea.

Es la inversión del camino de siempre. Con un Vocero de un solo negocio, Meta
manda el webhook a Nea y Nea lo relaya al CRM. Con un Vocero multitenant el
mensaje ya está guardado en la bandeja antes de que Nea lo vea, y el CRM se lo
entrega firmado.

Eso cambia tres cosas, y las tres para bien:

- **No hay relay.** El CRM ya tiene el mensaje; reenviárselo sería duplicarlo.
- **No hay coalesce aquí.** El CRM ya esperó a que el cliente terminara de
  escribir y entrega la ráfaga junta. Volver a esperar en Nea le sumaría
  segundos de silencio a un cliente que ya esperó los del CRM.
- **No se parsea a Meta.** Llega un evento normalizado, igual para WhatsApp que
  para Instagram que para lo que venga después.

Esta ruta solo existe con `VOCERO_MODE=cloud`. Sin esa bandera, Nea no la monta
y su webhook de Meta sigue siendo el de siempre.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.multiorg import organizacion_del_despacho
from app.state import AppContext, InboundMessage
from app.turn import handle_flush

logger = logging.getLogger("nea.dispatch")

router = APIRouter()

_bg_tasks: set[asyncio.Task[Any]] = set()


def firma_valida(body: bytes, header: str | None, secret: str) -> bool:
    """HMAC-SHA256 sobre el cuerpo CRUDO, con prefijo `sha256=`.

    Sobre el cuerpo crudo y no sobre el JSON re-serializado: un espacio de
    diferencia y la firma no coincide nunca. Es el error clásico de esta clase
    de integración, y por eso se valida antes de parsear nada.

    Sin secreto configurado se RECHAZA. Es lo contrario de lo que hace el
    webhook de Meta —donde un secreto vacío significa "dev, no verifiques"—, y
    la diferencia es deliberada: aquí el secreto es la única prueba de que
    quien despacha es el CRM y no cualquiera que conozca la URL.
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

    out: list[InboundMessage] = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        out.append(
            InboundMessage(
                wa_message_id=str(m.get("id")) if m.get("id") else None,
                identity=identity,
                type=str(m.get("type") or "text"),
                text=m.get("text"),
                profile_name=contacto.get("displayName"),
                media_id=m.get("mediaId"),
                media_mime=m.get("mimeType"),
                media_caption=m.get("caption"),
            )
        )
    return out


@router.post("/vocero/dispatch")
async def recibir(request: Request) -> Any:
    """Acusa recibo YA y procesa fuera de la ruta.

    El CRM reintenta con backoff si tardamos, así que responder rápido no es
    una optimización: es lo que evita procesar el mismo mensaje tres veces.
    """
    ctx: AppContext = request.app.state.ctx
    body = await request.body()

    if not firma_valida(
        body,
        request.headers.get("x-vocero-signature"),
        ctx.settings.crm_brain_secret,
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

    task = asyncio.create_task(_procesar(ctx, payload))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"status": "ok"}


async def _procesar(ctx: AppContext, payload: dict[str, Any]) -> None:
    dispatch_id = str(payload.get("dispatchId") or "")
    conversation = payload.get("conversation") or {}
    conversation_id = str(conversation.get("id") or "")

    mensajes = mensajes_del_despacho(payload)
    if not mensajes or not conversation_id:
        logger.warning("despacho %s sin mensajes o sin conversación", dispatch_id)
        return
    identity = mensajes[0].identity

    # Dedup por despacho, no por mensaje: el CRM reintenta el DESPACHO entero
    # cuando no le acusamos recibo, y contar sus mensajes uno por uno dejaría
    # pasar el segundo intento con la mitad de la ráfaga ya marcada.
    if dispatch_id:
        fresco = await ctx.store.mark_processed(f"dsp:{dispatch_id}")
        if not fresco:
            logger.info("dedup: despacho %s ya procesado — ignorado", dispatch_id)
            return

    if ctx.settings.multi_org:
        ctx_turno = await _contexto_multiorg(ctx, payload, identity, conversation_id)
        if ctx_turno is None:
            return  # el motivo ya quedó en el log, y la conversación con humano
    else:
        # El CRM ya sabe de quién es esta conversación; el cliente lo recuerda
        # para no preguntarlo por teléfono, que en esta superficie ni se puede.
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

    # Directo al turno, sin coalescer: el CRM ya agrupó la ráfaga.
    await handle_flush(ctx_turno, identity, mensajes)


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

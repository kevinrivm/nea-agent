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
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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

    # El CRM ya sabe de quién es esta conversación; el cliente lo recuerda para
    # no tener que preguntarlo por teléfono, que en esta superficie ni se puede.
    registrar = getattr(ctx.crm, "registrar", None)
    if callable(registrar):
        registrar(identity, conversation_id)

    # Directo al turno, sin coalescer: el CRM ya agrupó la ráfaga.
    await handle_flush(ctx, identity, mensajes)

"""Webhook de Meta: GET de verificación + POST de eventos.

Reglas duras:
- El POST responde 200 en <1 s SIEMPRE; el procesamiento es asíncrono.
- Firma `x-hub-signature-256` verificada si META_APP_SECRET está configurado
  (inválida o ausente → 401). Sin secret → se acepta (dev).
- El body crudo se encola para el relay al CRM ANTES de cualquier parseo.
- Dedup por `wa_message_id` (INSERT ... ON CONFLICT como gate atómico).
- Identidad: la misma forma en que el CRM guarda al contacto — el teléfono
  canónico, o `bsuid:<id>` si Meta solo manda el BSUID (`identidad_del_mensaje`).
  Sin ninguna → log + descarte.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import canonical_identity
from app.state import AppContext, InboundMessage

logger = logging.getLogger("nea.webhook")

router = APIRouter()

# Referencias vivas a las tareas de fondo (evita que el GC las mate a medias).
_bg_tasks: set[asyncio.Task[None]] = set()


def verify_signature(body: bytes, header: str | None, secret: str | None) -> bool:
    """HMAC-SHA256 del body crudo contra el app secret de Meta."""
    if not secret:
        return True  # sin secret configurado no se exige firma (dev)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):].lower(), expected)


def _extract_text(msg: dict[str, Any]) -> str | None:
    """Texto útil del mensaje; None si es multimedia u otro tipo."""
    mtype = msg.get("type")
    if mtype == "text":
        body = (msg.get("text") or {}).get("body")
        return str(body) if body else None
    if mtype == "button":
        text = (msg.get("button") or {}).get("text")
        return str(text) if text else None
    if mtype == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        title = reply.get("title")
        return str(title) if title else None
    return None


def capture_nontext(payload: dict[str, Any]) -> int:
    """Loguea el JSON crudo de cada mensaje no-texto (spec 002, tras el flag
    CAPTURE_PAYLOADS). Devuelve cuántos capturó — solo para tests."""
    captured = 0
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            for msg in ((change or {}).get("value") or {}).get("messages") or []:
                if isinstance(msg, dict) and msg.get("type") != "text":
                    logger.info(
                        "CAPTURE %s", json.dumps(msg, ensure_ascii=False)
                    )
                    captured += 1
    return captured


# El prefijo con el que el CRM guarda a un contacto que solo tiene BSUID
# (vocero-crm, src/server/inbox/identity.ts: BSUID_PREFIX).
BSUID_PREFIX = "bsuid:"


def identidad_del_mensaje(msg: dict[str, Any], contacts: list[Any]) -> str | None:
    """Quién escribió, en la MISMA forma en que el CRM guarda al contacto.

    Espejo de `resolveIdentity` del CRM: con teléfono (`from`), el teléfono
    canónico (521→52 MX); sin teléfono —quien usa nombre de usuario en
    WhatsApp y Meta solo identifica por su BSUID—, `bsuid:<id>`, sacado de
    `from_user_id` o, si no viene, del primer `user_id` de `contacts`.

    Nea usaba el BSUID pelón y el CRM lo guarda con prefijo: `/api/bot/context`
    daba 404 y a ese lead nunca se le contestaba. Canónica desde el origen:
    coalesce, BD, allowlist y seguimiento heredan la misma.
    """
    telefono = msg.get("from")
    if telefono:
        return canonical_identity(str(telefono))
    bsuid = msg.get("from_user_id") or next(
        (
            c.get("user_id")
            for c in contacts
            if isinstance(c, dict) and c.get("user_id")
        ),
        None,
    )
    if not bsuid:
        return None
    bsuid = str(bsuid).strip()
    return bsuid if bsuid.startswith(BSUID_PREFIX) else f"{BSUID_PREFIX}{bsuid}"


def extract_inbound(payload: dict[str, Any]) -> list[InboundMessage]:
    """Parseo tolerante del payload de Meta. Nunca truena por formato raro."""
    out: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if (change or {}).get("field") == "smb_message_echoes":
                # 008: echoes de coexistence (mensajes que el dueño mandó a
                # mano desde la app del teléfono). NO son entrantes de un lead
                # — solo relay al CRM, jamás abren turno de Nea.
                continue
            value = (change or {}).get("value") or {}
            contacts = value.get("contacts") or []
            profile_name = None
            if contacts and isinstance(contacts[0], dict):
                profile_name = (contacts[0].get("profile") or {}).get("name")
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                # Identidad resiliente: teléfono o BSUID — jamás truena sin wa_id.
                identity = identidad_del_mensaje(msg, contacts)
                if not identity:
                    logger.warning(
                        "mensaje %s sin identidad (ni from ni BSUID) — descartado",
                        msg.get("id"),
                    )
                    continue
                mtype = str(msg.get("type") or "unknown")
                if mtype == "reaction":
                    # Una reacción no abre turno (solo relay al CRM).
                    continue
                if mtype == "unsupported" and msg.get("errors"):
                    # Fantasma de Meta (p.ej. 131060 "message unavailable"):
                    # llega sin contenido y ~200 ms después Meta RE-ENTREGA el
                    # mensaje real con el mismo wamid. No abrir turno NI marcar
                    # dedup — si se marca, la re-entrega (con texto y referral)
                    # se descarta y Nea contesta "no puedo ver eso" a un texto
                    # normal (visto en vivo 2026-07-30 con un lead real).
                    logger.info(
                        "mensaje %s unsupported con errors de Meta — ignorado "
                        "(se espera re-entrega)",
                        msg.get("id"),
                    )
                    continue
                media = (
                    msg.get(mtype)
                    if mtype in ("audio", "image", "video", "document", "sticker")
                    else None
                ) or {}
                contact_names = []
                if mtype == "contacts":
                    for c in msg.get("contacts") or []:
                        if isinstance(c, dict):
                            name = (c.get("name") or {}).get("formatted_name") or (
                                c.get("name") or {}
                            ).get("first_name")
                            if name:
                                contact_names.append(str(name))
                referral = (msg.get("referral") or {}).get("headline")
                out.append(
                    InboundMessage(
                        wa_message_id=msg.get("id"),
                        identity=str(identity),
                        type=mtype,
                        text=_extract_text(msg),
                        referral_headline=str(referral) if referral else None,
                        profile_name=str(profile_name) if profile_name else None,
                        media_id=str(media["id"]) if media.get("id") else None,
                        media_mime=media.get("mime_type"),
                        media_filename=media.get("filename"),
                        media_caption=media.get("caption"),
                        media_voice=bool(media.get("voice")),
                        location=(
                            msg.get("location") if mtype == "location" else None
                        ),
                        contact_names=contact_names,
                    )
                )
    return out


@router.get("/webhook")
async def verify(request: Request) -> PlainTextResponse:
    """Verificación de suscripción de Meta (hub.challenge)."""
    ctx: AppContext = request.app.state.ctx
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge") or ""
    if mode == "subscribe" and token and token == ctx.settings.verify_token:
        return PlainTextResponse(challenge)
    logger.warning("verificación del webhook con token inválido")
    return PlainTextResponse("verify token inválido", status_code=403)


@router.post("/webhook")
async def receive(request: Request) -> Any:
    """200 inmediato; todo el trabajo real corre en una tarea de fondo."""
    ctx: AppContext = request.app.state.ctx
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not verify_signature(body, signature, ctx.settings.meta_app_secret or None):
        logger.warning("firma inválida o ausente en el webhook — 401")
        return JSONResponse({"error": "firma inválida"}, status_code=401)

    task = asyncio.create_task(_process(ctx, body, signature))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"status": "ok"}


async def _process(ctx: AppContext, body: bytes, signature: str | None) -> None:
    # 1) Relay primero: el CRM recibe el payload crudo pase lo que pase.
    try:
        await ctx.store.enqueue_relay(body, signature)
        ctx.relay_wake.set()
    except Exception:
        logger.exception("no pude encolar el relay — se pierde este payload")

    # 2) Parseo tolerante + dedup + coalesce.
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        logger.warning("payload del webhook no es JSON — solo relay")
        return
    if not isinstance(payload, dict):
        return
    if ctx.settings.capture_payloads:
        try:
            logger.info("CAPTURE_FULL %s", json.dumps(payload, ensure_ascii=False))
            capture_nontext(payload)
        except Exception:
            logger.exception("captura de payload falló — sigo")
    try:
        inbound = extract_inbound(payload)
    except Exception:
        logger.exception("parseo del payload falló — solo relay")
        return
    typing_identities: set[str] = set()
    for msg in inbound:
        if msg.wa_message_id:
            fresh = await ctx.store.mark_processed(msg.wa_message_id)
            if not fresh:
                logger.info("dedup: %s ya procesado — ignorado", msg.wa_message_id)
                continue
        if ctx.coalescer is None:
            logger.error("coalescer no inicializado — mensaje descartado")
            continue
        ctx.coalescer.add(msg.identity, msg)
        typing_identities.add(msg.identity)

    # "Escribiendo…" casi inmediato (antes de que cierre el coalesce): la señal
    # de vida no espera los segundos de recolección de ráfaga.
    for identity in typing_identities:
        task = asyncio.create_task(_early_typing(ctx, identity))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


async def _early_typing(ctx: AppContext, identity: str) -> None:
    """Marca leído + "escribiendo…" ~medio segundo tras recibir el mensaje.

    Best-effort absoluto. Solo cuando ya conocemos la conversación del CRM
    (primer contacto: lo cubre el typing del turno) y pasando la allowlist —
    a quien el bot no va a responder, tampoco le miente con un "escribiendo".
    El CRM además lo omite si la IA está pausada (handoff).
    """
    try:
        await asyncio.sleep(ctx.settings.typing_delay_seconds)
        allowed = ctx.settings.allowed_identities
        if allowed and canonical_identity(identity) not in allowed:
            return
        conv = await ctx.store.get_or_create_conversation(identity)
        if not conv.crm_conversation_id:
            return
        # Conversación ya cerrada por falta de rumbo: al relleno no se le
        # contesta, y un "escribiendo…" sería justo la mentira que este gate
        # evita. Si el mensaje trae contenido y la reabre, el "escribiendo…"
        # lo manda el turno, ya reabierta.
        if conv.stalled_at is not None:
            return
        await ctx.crm.post_typing(str(conv.crm_conversation_id))
    except Exception as exc:
        logger.debug("typing temprano de %s falló (%s) — sigo", identity, exc)

"""Orquestación del turno conversacional.

Gate → contexto del CRM → LLM con tools → envío vía CRM → ficha/fase/seguimiento.
Degradación silenciosa: cualquier fallo termina en silencio + log (y handoff
`error` si el LLM se agotó) — jamás texto roto al lead (Constitución IV).
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from app import media
from app.config import canonical_identity
from app.crm import CrmConflict, CrmError
from app.hostility import ALERT as HOSTILITY_ALERT, hostile_streak
from app.llm import LlmExhausted
from app.stall import ALERTA as STALL_ALERT, racha_vacia, sin_rumbo
from app.profile import resolve_profile
from app.prompt import build_system_prompt
from app.state import AppContext, InboundMessage, utcnow
from app.tools import ToolRuntime, tool_schemas

logger = logging.getLogger("nea.turn")

MAX_TOOL_ROUNDS = 5
# Cuánto calla el agente tras cerrar por falta de rumbo. Un lead que vuelve al
# día siguiente merece respuesta; el que insiste en el mismo hilo muerto, no.
STALL_COOLDOWN = timedelta(hours=24)
# Mensajes que se traen para contar el hilo del lead (el LLM ve menos).
STALL_LOOKBACK = 40
CONTEXT_ATTEMPTS = 3  # el relay puede tardar un instante en aterrizar en el CRM

# Comando de pruebas: reinicia la memoria de ESA conversación. Disponible SOLO
# para identidades de TESTER_WA_IDS (vacía = comando apagado).
RESET_COMMANDS = frozenset({"/reset", "#reset"})


def _agent_tz(settings: Any) -> ZoneInfo:
    try:
        return ZoneInfo(getattr(settings, "agent_timezone", "") or "America/Mexico_City")
    except Exception:
        logger.warning("AGENT_TIMEZONE inválida %r — uso America/Mexico_City",
                       getattr(settings, "agent_timezone", None))
        return ZoneInfo("America/Mexico_City")


@asynccontextmanager
async def conversation_lock(ctx: AppContext, identity: str) -> AsyncIterator[None]:
    """Serializa los turnos de UNA conversación.

    El coalescer agrupa ráfagas por debounce, pero nada le impide disparar un
    turno nuevo mientras el anterior sigue corriendo: el mensaje que llega
    tarde abre su propio turno con el contexto de ANTES de que el turno vivo
    actuara. Así se reserva una cita sin haber leído el mensaje que la
    corregía, y salen dos respuestas pisándose.

    Con el candado, el turno tardío espera, y al arrancar re-lee el contexto
    del CRM y el historial — que ya incluyen lo que hizo el turno anterior.
    """
    lock = ctx.turn_locks.get(identity)
    if lock is None:
        lock = ctx.turn_locks[identity] = asyncio.Lock()
    # El conteo sube ANTES del await: quien ya tiene el objeto en mano queda
    # contado, así que el candado nunca se recicla debajo de un turno que
    # espera (y el diccionario no crece sin fin con cada lead histórico).
    ctx.turn_lock_users[identity] = ctx.turn_lock_users.get(identity, 0) + 1
    if lock.locked():
        logger.info(
            "turno de %s en vuelo — el mensaje nuevo espera su turno", identity
        )
    try:
        async with lock:
            yield
    finally:
        remaining = ctx.turn_lock_users.get(identity, 1) - 1
        if remaining <= 0:
            ctx.turn_lock_users.pop(identity, None)
            ctx.turn_locks.pop(identity, None)
        else:
            ctx.turn_lock_users[identity] = remaining


async def handle_flush(ctx: AppContext, identity: str, items: list[Any]) -> None:
    """Callback del coalescer — nunca propaga excepciones."""
    try:
        async with conversation_lock(ctx, identity):
            await run_turn(ctx, identity, items)
    except Exception:
        logger.exception("turno de %s reventó — silencio", identity)


async def run_turn(
    ctx: AppContext, identity: str, inbound: list[InboundMessage]
) -> None:
    settings = ctx.settings

    # --- Gate 1: allowlist de pruebas (Constitución V) --------------------
    allowed = settings.allowed_identities
    if allowed and canonical_identity(identity) not in allowed:
        logger.info(
            "allowlist: %s fuera de ALLOWED_WA_IDS — relay sí, respuesta no", identity
        )
        return

    conv = await ctx.store.get_or_create_conversation(identity)

    # --- Comando /reset (líneas de prueba) --------------------------------
    # Corre ANTES de los gates de aiEnabled/ventana: un reset también debe
    # sacar la conversación de un handoff activo.
    if canonical_identity(identity) in settings.tester_identities and any(
        (m.text or "").strip().lower() in RESET_COMMANDS for m in inbound
    ):
        await _run_reset(ctx, conv, identity)
        return

    # --- Gate 1.5: conversación ya cerrada por no ir a ningún lado --------
    # El agente ya se despidió amable; seguir contestando es perseguir. Se
    # reabre sola tras el enfriamiento (un lead que vuelve al día siguiente
    # merece respuesta) o cuando el dueño reactiva la IA desde el CRM.
    if conv.stalled_at is not None:
        if utcnow() - conv.stalled_at < STALL_COOLDOWN:
            logger.info(
                "turno %s: conversación cerrada por falta de rumbo — silencio",
                identity,
            )
            return
        logger.info(
            "turno %s: el lead volvió tras el enfriamiento — reabro", identity
        )
        await ctx.store.update_conversation(conv.id, stalled_at=None)
        conv.stalled_at = None

    # --- Gate 2: contexto del CRM (aiEnabled, ventana) --------------------
    context = await _fetch_context(ctx, identity)
    if context is None:
        logger.warning("turno %s: sin contexto del CRM — silencio", identity)
        return
    conversation_info = context.get("conversation") or {}
    crm_conv_id = conversation_info.get("id")
    if not crm_conv_id:
        logger.warning("turno %s: contexto sin conversationId — silencio", identity)
        return
    if not conversation_info.get("aiEnabled", False):
        logger.info("turno %s: aiEnabled=false (handoff activo) — silencio", identity)
        return
    if not conversation_info.get("windowOpen", False):
        logger.info("turno %s: ventana de 24 h cerrada — silencio", identity)
        return

    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id=str(crm_conv_id),
        last_inbound_at=utcnow(),
        followup_due_at=None,  # el lead habló: se re-agenda al final del turno
    )

    # Señal de vida: leído + "escribiendo…" mientras Nea piensa (007).
    # Best-effort absoluto: un fallo aquí jamás afecta el turno.
    try:
        await ctx.crm.post_typing(str(crm_conv_id))
    except Exception as exc:
        logger.debug("typing de %s falló (%s) — sigo", identity, exc)

    # --- Contenido del turno: texto + multimedia procesada (spec 002) -----
    parts: list[str] = []
    image_uris: list[str] = []
    for m in inbound:
        if m.text:
            parts.append(m.text)
            continue
        if m.type in ("text", "button", "interactive"):
            continue  # texto vacío raro: nada que procesar
        part = await media.describe_item(ctx, m)
        if part.text:
            parts.append(part.text)
        if part.image_data_uri:
            image_uris.append(part.image_data_uri)
    if not parts and not image_uris:
        logger.info("turno %s: nada procesable en la ráfaga — silencio", identity)
        return

    user_text = "\n".join(parts)
    await ctx.store.add_message(
        conv.id, "user", user_text, wa_message_id=inbound[0].wa_message_id
    )

    # --- Armar mensajes para el LLM ---------------------------------------
    referral = next((m.referral_headline for m in inbound if m.referral_headline), None)
    offered = await ctx.store.get_offered_slots(conv.id)
    profile = await resolve_profile(ctx)
    system = build_system_prompt(
        profile=profile,
        context=context,
        conv=conv,
        referral_headline=referral,
        offered=offered,
        agenda=ctx.agenda_enabled,
        tz=_agent_tz(settings),
    )
    # Se traen más mensajes de los que ve el LLM: el candado de cierre cuenta
    # el hilo COMPLETO del lead, no solo la ventana de contexto.
    recientes = await ctx.store.recent_messages(conv.id, STALL_LOOKBACK)
    history = recientes[-settings.history_window :]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] + [
        {"role": m.role, "content": m.content} for m in history
    ]
    # Hostilidad sostenida (AC-18): el CONTEO es determinista — el LLM salió
    # flaky contando entre turnos. Al tercer strike: alerta en el turno y
    # handoff garantizado más abajo aunque el modelo no llame la herramienta.
    streak = hostile_streak([m.content for m in history if m.role == "user"])
    if streak >= 3:
        messages.append({"role": "system", "content": HOSTILITY_ALERT})
    # Candado de cierre: conversación que no va a ningún lado. Se despide con
    # UNA línea cálida en este turno y después calla (gate 1.5). El conteo es
    # determinista aquí; el LLM solo pone la redacción.
    del_lead = [m.content for m in recientes if m.role == "user"]
    cerrar_sin_rumbo = streak < 3 and sin_rumbo(del_lead, conv.phase)
    if cerrar_sin_rumbo:
        logger.info(
            "turno %s: sin rumbo (%d mensajes del lead, racha vacía %d) — cierro",
            identity,
            len(del_lead),
            racha_vacia(del_lead),
        )
        messages.append({"role": "system", "content": STALL_ALERT})
    if image_uris:
        # El último user message de este turno se vuelve multimodal: el
        # historial persiste solo el texto; las imágenes viven en ESTE turno.
        last = messages[-1]
        last["content"] = [{"type": "text", "text": str(last["content"])}] + [
            {"type": "image_url", "image_url": {"url": uri}} for uri in image_uris
        ]

    # --- LLM con tools ----------------------------------------------------
    runtime = ToolRuntime(ctx, conv, str(crm_conv_id), profile=profile)
    try:
        final_text = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error(
            "turno %s: LLM agotó reintentos (%s) — silencio + handoff error",
            identity,
            exc,
        )
        await _safe_handoff(ctx, str(crm_conv_id), "error")
        await ctx.store.update_conversation(
            conv.id, phase="cerrada", followup_due_at=None
        )
        return

    # Backstop determinista: al tercer strike el handoff SUCEDE, lo haya
    # llamado el modelo o no (la regla de negocio no depende de su humor).
    if streak >= 3 and runtime.handoff_reason is None:
        runtime.handoff_reason = "hostilidad"

    # --- Enviar la respuesta (SIEMPRE vía el CRM, nunca Meta directo) -----
    sent = False
    if final_text and final_text.strip():
        sent = await _send(ctx, conv.id, str(crm_conv_id), final_text.strip())
        if sent:
            await ctx.store.add_message(conv.id, "assistant", final_text.strip())

    # El handoff se ejecuta DESPUÉS de la despedida (si no, el CRM la rechaza
    # con 409 ai_paused).
    if runtime.handoff_reason is not None:
        await _safe_handoff(ctx, str(crm_conv_id), runtime.handoff_reason)

    # --- Fase + seguimiento -----------------------------------------------
    updates: dict[str, Any] = {"greeted": True}
    if cerrar_sin_rumbo:
        # Se marca aunque el envío haya fallado: la decisión de cerrar ya se
        # tomó y no queremos que el próximo mensaje reabra el ciclo.
        updates["stalled_at"] = utcnow()
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    elif runtime.handoff_reason is not None or runtime.booked or runtime.routed_out:
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    else:
        if runtime.proposed:
            updates["phase"] = "agendando"
        if sent and not conv.followup_sent:
            updates["followup_due_at"] = utcnow() + timedelta(
                hours=settings.followup_hours
            )
    await ctx.store.update_conversation(conv.id, **updates)


async def _run_reset(ctx: AppContext, conv: Any, identity: str) -> None:
    """Reinicio de pruebas: CRM primero (ficha limpia + IA reactivada, para que
    la confirmación no rebote con 409 ai_paused) y luego la memoria local."""
    crm_conv_id = conv.crm_conversation_id
    if not crm_conv_id:
        context = await _fetch_context(ctx, identity)
        crm_conv_id = ((context or {}).get("conversation") or {}).get("id")
    if crm_conv_id:
        try:
            await ctx.crm.post_reset(str(crm_conv_id))
        except CrmError as exc:
            logger.warning("reset %s: el CRM no pudo reiniciar (%s) — sigo", identity, exc)
    await ctx.store.reset_conversation(conv.id)
    logger.info("reset de pruebas ejecutado para %s", identity)
    if crm_conv_id:
        await _send(
            ctx,
            conv.id,
            str(crm_conv_id),
            "🧹 Listo: memoria reiniciada. Te trato como lead nuevo desde tu "
            "próximo mensaje. (Comando de pruebas, solo líneas autorizadas.)",
        )


async def _fetch_context(ctx: AppContext, identity: str) -> dict[str, Any] | None:
    for attempt in range(CONTEXT_ATTEMPTS):
        try:
            context = await ctx.crm.get_context(identity)
        except CrmError as exc:
            logger.warning(
                "context de %s: error del CRM (intento %d): %s",
                identity,
                attempt + 1,
                exc,
            )
            context = None
        if context is not None:
            return context
        if attempt < CONTEXT_ATTEMPTS - 1:
            await asyncio.sleep(1.0)  # chance a que el relay aterrice en el CRM
    return None


async def _tool_loop(
    ctx: AppContext, messages: list[dict[str, Any]], runtime: ToolRuntime
) -> str | None:
    """Rondas de tool-calling hasta obtener texto final (o rendirse)."""
    for _ in range(MAX_TOOL_ROUNDS):
        reply = await ctx.llm.complete(
            messages, tools=tool_schemas(ctx.agenda_enabled)
        )
        if not reply.tool_calls:
            return reply.content  # turno de puro texto
        # content vacío con tool_calls es normal (turno solo-herramientas)
        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in reply.tool_calls
                ],
            }
        )
        for tc in reply.tool_calls:
            result = await runtime.execute(tc.name, tc.arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
    logger.warning("turno: demasiadas rondas de herramientas — corto sin texto")
    return None


SEND_ATTEMPTS = 4  # backoff 1 s, 2 s, 4 s entre intentos (~7 s en el turno)


async def _send(ctx: AppContext, conv_id: int, crm_conv_id: str, text: str) -> bool:
    """Envía vía el CRM. Si el turno agota sus reintentos, la respuesta NO se
    descarta: se encola en pending_send y el SenderWorker la reintenta con
    backoff hasta entregar o agotar 24 h (incidente 2026-08-03)."""
    for attempt in range(SEND_ATTEMPTS):
        try:
            await ctx.crm.send_message(crm_conv_id, text)
            return True
        except CrmConflict as exc:
            # ai_paused / window_closed: silencio respetuoso, sin reintento.
            logger.info("envío bloqueado por el CRM (%s) — silencio", exc.code)
            return False
        except CrmError as exc:
            logger.warning("envío falló (intento %d): %s", attempt + 1, exc)
            if attempt < SEND_ATTEMPTS - 1:
                await asyncio.sleep(2.0**attempt)
    pending_id = await ctx.store.enqueue_pending_send(conv_id, crm_conv_id, text)
    logger.error(
        "envío agotó reintentos del turno — encolado como pending_send %d",
        pending_id,
    )
    return False


async def _safe_handoff(ctx: AppContext, crm_conv_id: str, reason: str) -> None:
    try:
        await ctx.crm.post_handoff(crm_conv_id, reason)
        logger.info("handoff registrado en el CRM (reason=%s)", reason)
    except CrmError as exc:
        logger.error("no pude registrar el handoff (%s): %s", reason, exc)

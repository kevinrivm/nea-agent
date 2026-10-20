"""Orquestación del turno conversacional.

Gate → contexto del CRM → LLM con tools → envío vía CRM → ficha/fase/seguimiento.
Degradación silenciosa: cualquier fallo termina en silencio + log (y handoff
`error` si el LLM se agotó) — jamás texto roto al lead (Constitución IV).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from app import media
from app.agenda import agenda_vigente
from app.config import canonical_identity
from app.crm import CrmConflict, CrmError, CrmUnreachable
from app.formato import a_whatsapp
from app.hostility import ALERT as HOSTILITY_ALERT, hostile_streak
from app.llm import LlmExhausted
from app.stall import (
    ALERTA as STALL_ALERT,
    FICHA_CIERRE,
    racha_vacia,
    sin_rumbo,
    trae_contenido,
)
from app.profile import resolve_profile
from app.prompt import build_system_prompt, zona_del_negocio
from app.state import AppContext, BotMessage, InboundMessage, utcnow
from app.tools import ToolRuntime, tool_schemas

logger = logging.getLogger("nea.turn")

MAX_TOOL_ROUNDS = 5
# Mensajes que se traen, como mínimo, para contar el hilo del lead (el LLM ve
# menos). Si STALL_MAX_TURNS pide contar más de lo que cabe (un mensaje del
# lead por cada dos), se traen más; con los valores por defecto, 40 como siempre.
STALL_LOOKBACK = 40
CONTEXT_ATTEMPTS = 3  # un hipo de red del CRM no le cuesta la respuesta al lead
CONTEXT_PAUSE_SECONDS = 1.0  # entre esos intentos
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

    El CRM agrupa la ráfaga antes de despacharla, pero nada le impide despachar
    otra mientras el turno anterior sigue corriendo: el mensaje que llega
    tarde abriría su propio turno con el contexto de ANTES de que el turno vivo
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


@dataclass
class _Turno:
    """Lo que un turno sabe de sí mismo, para la red de seguridad.

    `run_turn` lo va llenando mientras avanza; si revienta, `handle_flush` lo
    lee para saber a qué conversación del CRM avisarle y si ya se la había
    pasado a un humano.
    """

    # La conversación del CRM que este turno tomó: la que trajo el despacho
    # o la del contexto, una vez pasados los gates.
    crm_conversation_id: str | None = None
    # El motivo del handoff que este turno ya registró, si registró uno.
    handoff: str | None = None


class TurnoSinCrm(Exception):
    """El turno no alcanzó al CRM al leer su contexto (gate 2).

    Sube hasta la cola del despacho (app/dispatch_worker.py), que le pasa la
    conversación a un humano —cuando el CRM vuelva a contestar, si tampoco
    contesta para eso—. Nea 1.x reintentaba aquí la ráfaga hasta diez minutos
    porque el relay podía no haberle entregado todavía el mensaje al CRM; con
    el despacho, el CRM acaba de mandarlo y ya lo tiene.
    """


def _marcas(items: list[Any]) -> list[str]:
    return [str(w) for w in (getattr(m, "wa_message_id", None) for m in items) if w]


def marca_de_leido(org_id: str, message_id: str) -> str:
    """La marca de un mensaje del CRM que ya entró al historial de Nea.

    Con la organización dentro: una Nea atiende a varios negocios, y el id de
    un mensaje es de SU CRM. El prefijo la separa de las marcas de despacho
    (`dsp:`) y de las del webhook de Nea 1.x (el wamid pelón).
    """
    return f"msg:{org_id}:{message_id}"


async def _anotar_leidos(ctx: AppContext, inbound: list[Any]) -> None:
    """Deja dicho qué mensajes del CRM ya leyó Nea.

    El CRM —la raíz y cloud— puede entregar el mismo mensaje en DOS despachos: lo que el cliente
    escribe mientras uno va en vuelo sale en otro, con otro `dispatchId`, que
    repite la ráfaga. El dedup por despacho no lo ve, y el mensaje repetido
    entraba otra vez al historial.

    Se anota AQUÍ, cuando el mensaje entra al historial, y no al recibir el
    despacho: un turno que un gate calló (IA apagada, ventana cerrada) no leyó
    nada, y si el CRM vuelve a mandar esos mensajes hay que leerlos.
    """
    org_id = (ctx.organizacion or ("", ""))[0]
    for marca in _marcas(inbound):
        await ctx.store.mark_processed(marca_de_leido(org_id, marca))


async def handle_flush(
    ctx: AppContext,
    identity: str,
    items: list[Any],
    crm_conversation_id: str | None = None,
    *,
    propagate_errors: bool = False,
) -> None:
    """El turno de un despacho. Sin `propagate_errors`, nunca lanza.

    Un turno que revienta por algo inesperado —un fallo de código, la base
    caída a medio turno, un CRM que contesta algo que no es JSON— dejaba al
    lead sin respuesta y sin nadie que lo viera: solo quedaba el log. Ahora,
    además del log (con los ids de la ráfaga, para encontrarla), se registra
    un handoff `error` en el CRM si se sabe de qué conversación era, para que
    un humano la atienda. El handoff va por `ctx.crm`, el cliente del turno:
    `/api/bot` contra la raíz y, en cloud, `/api/brains` con la credencial de
    ESA organización — igual que el handoff normal.

    `crm_conversation_id` lo pasa el despacho, donde el CRM ya dijo qué
    conversación es. La cola del despacho llama con `propagate_errors`: el
    error sube y es ella la que decide (app/dispatch_worker.py).
    """
    turno = _Turno(crm_conversation_id=crm_conversation_id or None)
    try:
        async with conversation_lock(ctx, identity):
            await run_turn(ctx, identity, items, turno)
    except Exception:
        if propagate_errors:
            raise
        logger.exception(
            "turno de %s reventó (ids: %s) — silencio",
            identity,
            ", ".join(_marcas(items)) or "ninguno",
        )
        await _handoff_de_emergencia(ctx, identity, turno)


async def _handoff_de_emergencia(ctx: AppContext, identity: str, turno: _Turno) -> None:
    """La red de seguridad de `handle_flush`. Best-effort: nunca lanza.

    No registra un segundo handoff encima de uno que el turno ya hizo: el
    motivo es lo que el dueño lee en su bandeja, y pisar un «el cliente pidió
    humano» con un «error» le diría otra cosa.
    """
    if turno.handoff is not None:
        logger.info(
            "turno de %s: ya se había pasado a un humano (%s) — sin otro handoff",
            identity,
            turno.handoff,
        )
        return
    if not turno.crm_conversation_id:
        logger.warning(
            "turno de %s: no sé de qué conversación del CRM era — sin handoff",
            identity,
        )
        return
    try:
        await ctx.crm.post_handoff(turno.crm_conversation_id, "error")
    except Exception as exc:
        logger.error(
            "turno de %s: tampoco pude registrar el handoff error en %s (%s)",
            identity,
            turno.crm_conversation_id,
            exc,
        )
        return
    logger.info(
        "turno de %s: handoff error registrado en %s tras el fallo",
        identity,
        turno.crm_conversation_id,
    )


async def run_turn(
    ctx: AppContext,
    identity: str,
    inbound: list[InboundMessage],
    turno: _Turno | None = None,
) -> None:
    turno = turno if turno is not None else _Turno()
    settings = ctx.settings

    # --- Gate 1: allowlist de pruebas (Constitución V) --------------------
    if not settings.identidad_permitida(identity):
        logger.info(
            "allowlist: %s fuera de ALLOWED_WA_IDS — sin respuesta", identity
        )
        return

    # La conversación es de una ORGANIZACIÓN, no solo de una identidad. Con
    # una Nea de un solo negocio la tupla es vacía y todo queda igual que
    # siempre; con varias, es lo que impide que el mismo teléfono escribiendo
    # a dos negocios comparta historial. Ver migrations/004_multiorg.sql.
    org_id, org_slug = ctx.organizacion or ("", "")
    conv = await ctx.store.get_or_create_conversation(identity, org_id, org_slug)
    # El despacho ya dijo de qué conversación del CRM es este turno: si no
    # es la que Nea recordaba, se olvida ANTES de los gates de abajo,
    # que leen esa memoria (un cierre sin rumbo viejo callaría el primer
    # mensaje de la conversación nueva).
    conv = await _olvidar_si_es_otra(ctx, conv, turno.crm_conversation_id, identity)

    # --- Comando /reset (líneas de prueba) --------------------------------
    # Corre ANTES de los gates de aiEnabled/ventana: un reset también debe
    # sacar la conversación de un handoff activo.
    if canonical_identity(identity) in settings.tester_identities and any(
        (m.text or "").strip().lower() in RESET_COMMANDS for m in inbound
    ):
        await _run_reset(ctx, conv, identity)
        return

    # --- Gate 1.5: conversación ya cerrada por no ir a ningún lado --------
    # El agente ya se despidió amable; contestarle el relleno ("gracias",
    # "ok", un emoji) sería perseguir. Pero el cierre no es para siempre: un
    # mensaje con contenido la reabre en el acto —«¿cuánto cuesta?» merece
    # respuesta aunque llegue a los diez minutos— y, pasado el enfriamiento
    # (STALL_COOLDOWN_HOURS), la reabre cualquiera.
    if conv.stalled_at is not None:
        enfriando = utcnow() - conv.stalled_at < timedelta(
            hours=settings.stall_cooldown_hours
        )
        if enfriando and not any(trae_contenido(m.type, m.text) for m in inbound):
            logger.info(
                "turno %s: relleno tras el cierre por falta de rumbo — silencio",
                identity,
            )
            return
        logger.info(
            "turno %s: %s — reabro",
            identity,
            "el lead escribió algo con contenido"
            if enfriando
            else "el lead volvió tras el enfriamiento",
        )
        await _reabrir(ctx, conv, identity)

    # --- Gate 2: contexto del CRM (aiEnabled, ventana) --------------------
    # Un CRM que no contesta no es un «no»: la ráfaga se reintenta
    # (handle_flush). Solo el 404 de verdad termina en silencio.
    try:
        context = await _fetch_context(ctx, identity)
    except CrmUnreachable as exc:
        raise TurnoSinCrm(str(exc)) from exc
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
    # Sin despacho (el seguimiento) la conversación del CRM se conoce aquí.
    conv = await _olvidar_si_es_otra(ctx, conv, str(crm_conv_id), identity)
    # Desde aquí el turno es de Nea: si revienta, la red de seguridad de
    # handle_flush sabe a qué conversación del CRM avisarle.
    turno.crm_conversation_id = str(crm_conv_id)

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
    await _anotar_leidos(ctx, inbound)

    # --- Armar mensajes para el LLM ---------------------------------------
    # ¿El CRM agenda HOY? La respuesta caduca (app/agenda.py): si venció, este
    # turno la vuelve a pedir, con timeout corto. Así encender AGENDA en el
    # CRM llega sin reiniciar Nea, y un 404 de la bandera no apaga para siempre.
    ctx.agenda_enabled = await agenda_vigente(ctx)
    referral = next((m.referral_headline for m in inbound if m.referral_headline), None)
    offered = await ctx.store.get_offered_slots(conv.id)
    profile = await resolve_profile(ctx)
    # La zona es la del negocio de ESTE turno (la manda el CRM), no la de la
    # instancia: una Nea atiende a negocios de zonas distintas.
    tz = zona_del_negocio(context, _agent_tz(settings))
    system = build_system_prompt(
        profile=profile,
        context=context,
        conv=conv,
        referral_headline=referral,
        offered=offered,
        agenda=ctx.agenda_enabled,
        tz=tz,
        recordatorios=bool(getattr(ctx.crm, "supports_agenda_v2", False)),
        canal=next((m.channel for m in inbound if m.channel), None),
    )
    # Se traen más mensajes de los que ve el LLM: el candado de cierre cuenta
    # el hilo COMPLETO del lead, no solo la ventana de contexto.
    recientes = await ctx.store.recent_messages(
        conv.id, max(STALL_LOOKBACK, 2 * settings.stall_max_turns + 2)
    )
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
    # determinista aquí; el LLM solo pone la redacción. Cuenta solo lo de
    # después de la última reapertura: el hilo viejo ya tuvo su despedida.
    del_lead = [
        m.content
        for m in recientes
        if m.role == "user" and m.id > conv.stall_since_message_id
    ]
    cerrar_sin_rumbo = streak < 3 and sin_rumbo(
        del_lead,
        conv.phase,
        racha=settings.stall_filler_streak,
        max_mensajes=settings.stall_max_turns,
    )
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
    runtime = ToolRuntime(
        ctx,
        conv,
        str(crm_conv_id),
        profile=profile,
        tz=tz,
        conversacion_reciente=_conversacion_reciente(history),
    )
    try:
        final_text = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error(
            "turno %s: LLM agotó reintentos (%s) — silencio + handoff error",
            identity,
            exc,
        )
        await _safe_handoff(ctx, str(crm_conv_id), "error", turno)
        await ctx.store.update_conversation(
            conv.id, phase="cerrada", followup_due_at=None
        )
        return

    # Última red antes de enviar: el modelo repitió un mensaje que el lead ya
    # tiene, o escribió una nota para sí mismo. Se le pide UNA vez más; si
    # vuelve a salir mal, silencio + handoff error, igual que un LLM caído.
    previos = [m.content for m in recientes if m.role == "assistant"]
    motivo = _respuesta_invalida(final_text, previos)
    if motivo is not None:
        logger.warning("turno %s: la respuesta %s — pido otra", identity, motivo)
        messages.append({"role": "assistant", "content": final_text})
        messages.append({"role": "system", "content": CORRIGE_RESPUESTA.format(motivo=motivo)})
        try:
            final_text = await _tool_loop(ctx, messages, runtime)
        except LlmExhausted:
            final_text = None
        if final_text is None or _respuesta_invalida(final_text, previos):
            logger.error("turno %s: la segunda respuesta tampoco sirve — silencio + handoff error", identity)
            await _safe_handoff(ctx, str(crm_conv_id), "error", turno)
            await ctx.store.update_conversation(conv.id, phase="cerrada", followup_due_at=None)
            return

    # Backstop determinista: al tercer strike el handoff SUCEDE, lo haya
    # llamado el modelo o no (la regla de negocio no depende de su humor).
    if streak >= 3 and runtime.handoff_reason is None:
        runtime.handoff_reason = "hostilidad"

    # --- Enviar la respuesta (SIEMPRE vía el CRM, nunca Meta directo) -----
    # WhatsApp no pinta Markdown: se convierte aquí, y lo que se guarda en el
    # historial es lo ya convertido para que el modelo no aprenda de vuelta
    # el formato que el lead ve roto (app/formato.py).
    sent = False
    if final_text and final_text.strip():
        final_text = a_whatsapp(runtime.finalize_reply(final_text.strip()))
    if final_text and final_text.strip():
        sent = await _send(ctx, conv.id, str(crm_conv_id), final_text)
        if sent:
            await ctx.store.add_message(conv.id, "assistant", final_text)

    # El handoff se ejecuta DESPUÉS de la despedida (si no, el CRM la rechaza
    # con 409 ai_paused).
    if runtime.handoff_reason is not None:
        await _safe_handoff(ctx, str(crm_conv_id), runtime.handoff_reason, turno)

    # --- Fase + seguimiento -----------------------------------------------
    updates: dict[str, Any] = {"greeted": True}
    cerrada_en = utcnow()
    if cerrar_sin_rumbo:
        # Se marca aunque el envío haya fallado: la decisión de cerrar ya se
        # tomó y no queremos que el próximo mensaje reabra el ciclo.
        updates["stalled_at"] = cerrada_en
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    elif runtime.handoff_reason is not None or runtime.booked or runtime.routed_out:
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    else:
        if runtime.proposed:
            updates["phase"] = "agendando"
        if sent and not conv.followup_sent and not settings.cloud_mode:
            updates["followup_due_at"] = utcnow() + timedelta(
                hours=settings.followup_hours
            )
    await ctx.store.update_conversation(conv.id, **updates)
    if cerrar_sin_rumbo:
        # A la vista del dueño: en el panel del contacto sale «Cierre sin
        # rumbo» con la hora local. Sin esto, desde el CRM solo se veía a una
        # Nea que de pronto dejó de contestar.
        await _anotar_cierre(
            ctx,
            str(crm_conv_id),
            cerrada_en.astimezone(tz).isoformat(timespec="minutes"),
            identity,
        )


async def _reabrir(ctx: AppContext, conv: Any, identity: str) -> None:
    """Saca la conversación del candado de cierre, con los contadores en cero.

    La fase vuelve a descubrimiento: el cierre la deja en `cerrada`, y con
    ella el candado no se volvía a disparar nunca (ni había seguimiento). Los
    contadores no se borran: se mueve la marca desde la que cuentan (006) al
    último mensaje de la conversación, y el hilo viejo deja de contar.
    """
    ultimos = await ctx.store.recent_messages(conv.id, 1)
    desde = ultimos[-1].id if ultimos else 0
    await ctx.store.update_conversation(
        conv.id,
        stalled_at=None,
        phase="descubrimiento",
        stall_since_message_id=desde,
    )
    conv.stalled_at = None
    conv.phase = "descubrimiento"
    conv.stall_since_message_id = desde
    if conv.crm_conversation_id:
        await _anotar_cierre(ctx, str(conv.crm_conversation_id), None, identity)


async def _anotar_cierre(
    ctx: AppContext, crm_conv_id: str, valor: str | None, identity: str
) -> None:
    """Escribe (o borra, con `None`) el cierre en la ficha del CRM.

    Merge del CRM: la clave va sola y `null` la borra sin tocar el resto de la
    ficha. Best-effort absoluto: el candado funciona igual sin el CRM.
    """
    try:
        await ctx.crm.put_ficha(crm_conv_id, {FICHA_CIERRE: valor})
    except Exception as exc:
        logger.warning(
            "turno %s: no pude %s el cierre en la ficha (%s)",
            identity,
            "anotar" if valor else "borrar",
            exc,
        )


async def _olvidar_si_es_otra(
    ctx: AppContext, conv: Any, crm_conversation_id: str | None, identity: str
) -> Any:
    """La red de seguridad del borrado: memoria de OTRA conversación no se usa.

    Cuando alguien elimina una conversación en el CRM, este avisa (ver
    `app/dispatch.py`). Pero el aviso puede no llegar —Nea reiniciándose, un CRM
    que no avisa— y entonces lo único que delata el borrado es que la misma
    persona vuelve con una conversación de id distinto. Contestarle con el
    historial de la que se eliminó sería justo lo que el borrado prometió que
    no iba a pasar.
    """
    anterior = conv.crm_conversation_id
    if not crm_conversation_id or not anterior or anterior == crm_conversation_id:
        return conv
    logger.info(
        "turno %s: el CRM cambió la conversación (%s → %s) — empiezo de cero",
        identity,
        anterior,
        crm_conversation_id,
    )
    # Lo mismo que hace el aviso, y no un `reset_conversation`: aquel conserva
    # la fila y sus envíos pendientes, que aquí serían respuestas a una
    # conversación que ya no existe.
    org_id, org_slug = ctx.organizacion or ("", "")
    await ctx.store.forget_conversation(org_id, identity, anterior)
    return await ctx.store.get_or_create_conversation(identity, org_id, org_slug)


async def _run_reset(ctx: AppContext, conv: Any, identity: str) -> None:
    """Reinicio de pruebas: CRM primero (ficha limpia + IA reactivada, para que
    la confirmación no rebote con 409 ai_paused) y luego la memoria local."""
    crm_conv_id = conv.crm_conversation_id
    if not crm_conv_id:
        try:
            context = await _fetch_context(ctx, identity)
        except CrmUnreachable:
            context = None
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
    """El contexto del CRM, o None si el CRM dice que no conoce la conversación.

    Lanza `CrmUnreachable` si en el último intento el CRM no contestó (red,
    timeout, 5xx).
    """
    caida: CrmUnreachable | None = None
    for attempt in range(CONTEXT_ATTEMPTS):
        caida = None
        try:
            context = await ctx.crm.get_context(identity)
        except CrmUnreachable as exc:
            logger.warning(
                "context de %s: el CRM no contestó (intento %d): %s",
                identity,
                attempt + 1,
                exc,
            )
            context, caida = None, exc
        except CrmError as exc:
            logger.warning(
                "context de %s: error del CRM (intento %d): %s",
                identity,
                attempt + 1,
                exc,
            )
            context = None
        else:
            if context is None:
                return None  # 404 de verdad: no hay por qué insistir
        if context is not None:
            return context
        if attempt < CONTEXT_ATTEMPTS - 1:
            await asyncio.sleep(CONTEXT_PAUSE_SECONDS)
    if caida is not None:
        raise caida
    return None


REPETICION_MIN = 40
CORRIGE_RESPUESTA = (
    "Ese texto NO se le envió al lead: {motivo}. Escribe ahora el mensaje de "
    "WhatsApp que responde a su ÚLTIMO mensaje, en tu voz y sin repetir nada "
    "que ya le hayas escrito."
)


def _respuesta_invalida(texto: str | None, previos: list[str]) -> str | None:
    """Por qué este texto no puede salir, o None si puede.

    Dos fallos que el lead SÍ ve y que ningún prompt evita del todo: repetirle
    palabra por palabra un mensaje que ya tiene (en producción, el saludo del
    principio a media conversación) y mandarle una nota interna entre
    paréntesis («(Registro actualizado — conversación cerrada…)»).
    """
    if not texto or not texto.strip():
        return None
    # Se compara lo que el lead VERÍA: ya convertido a WhatsApp. Así un enlace
    # de Markdown suelto («[Agenda](https://…)») no pasa por nota entre
    # corchetes, y una repetición no se escapa por traer otras negritas que
    # el mensaje ya guardado (que se guardó convertido).
    plano = " ".join(a_whatsapp(texto).split())
    if re.fullmatch(r"[(\[].*[)\]]", plano):
        return "era una nota interna entre paréntesis"
    # Un «¡Va!» o un «Perfecto 👍» pueden repetirse sin que nadie lo note; un
    # mensaje con contenido repetido entero, no.
    if len(plano) >= REPETICION_MIN and any(
        plano == " ".join(a_whatsapp(p).split()) for p in previos
    ):
        return "repetía palabra por palabra un mensaje que ya le enviaste"
    return None


async def _tool_loop(
    ctx: AppContext, messages: list[dict[str, Any]], runtime: ToolRuntime
) -> str | None:
    """Rondas de tool-calling hasta obtener texto final (o rendirse).

    El texto que el modelo escribe JUNTO a una llamada de herramienta no entra
    al historial de la ronda siguiente. GLM, el modelo de cloud, suele
    escribir el mensaje y llamar la herramienta en la misma respuesta; al
    verse ese texto en el historial lo daba por enviado y la ronda siguiente
    salía de relleno: un «¡Éxito!» suelto, una nota interna entre paréntesis
    o el saludo del principio otra vez. Solo eso le llegaba al lead
    (aishiagency, 18 sep 2026; reproducido 8 de 11 veces).

    Tampoco se envía ese texto tal cual: la mitad de las veces es un
    preámbulo («Anoto eso 👌») que espera la ronda siguiente para hacer la
    pregunta. Sin él a la vista, el modelo escribe la respuesta completa ya
    con el resultado de la herramienta, que es lo que siempre hizo bien.
    """
    for _ in range(MAX_TOOL_ROUNDS):
        reply = await ctx.llm.complete(
            messages, tools=tool_schemas(ctx.agenda_enabled, bool(getattr(ctx.crm, "supports_agenda_v2", False)), bool(getattr(ctx.crm, "supports_coordination", False)))
        )
        if not reply.tool_calls:
            return reply.content  # turno de puro texto
        # content vacío con tool_calls es normal (turno solo-herramientas)
        messages.append(
            {
                "role": "assistant",
                "content": None,  # ver el docstring: no se enseña como ya dicho
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
    # La organización viaja con el encolado: cuando el SenderWorker despierte
    # —quizá horas después— tiene que hablarle al CRM con la credencial de
    # ESTA, y para entonces el turno ya no existe.
    org_id, org_slug = ctx.organizacion or ("", "")
    # Y el despacho que se estaba contestando: el CRM lo exige para responder,
    # y para cuando el worker despierte este turno ya no existe. El turno no lo
    # sabe —recibe conversación y texto—, se lo pide al cliente, que sí.
    despacho_de = getattr(ctx.crm, "despacho_de", None)
    dispatch_id = despacho_de(crm_conv_id) if callable(despacho_de) else ""
    pending_id = await ctx.store.enqueue_pending_send(
        conv_id, crm_conv_id, text, org_id, org_slug, dispatch_id
    )
    logger.error(
        "envío agotó reintentos del turno — encolado como pending_send %d",
        pending_id,
    )
    return False


async def _safe_handoff(
    ctx: AppContext, crm_conv_id: str, reason: str, turno: _Turno | None = None
) -> None:
    try:
        await ctx.crm.post_handoff(crm_conv_id, reason)
        logger.info("handoff registrado en el CRM (reason=%s)", reason)
    except CrmError as exc:
        logger.error("no pude registrar el handoff (%s): %s", reason, exc)
        return
    # Queda anotado para que la red de seguridad no registre otro encima.
    if turno is not None:
        turno.handoff = reason


def _conversacion_reciente(history: list[BotMessage]) -> str:
    """Lo que cuenta para saber si alguien nombró un día (app/dias.py).

    Los últimos mensajes del lead, y lo último que dijo Nea: si ella preguntó
    «¿te late el jueves?» y el lead contestó «sí», el jueves ya está pedido.
    """
    del_lead = [m.content for m in history if m.role == "user"][-3:]
    de_nea = [m.content for m in history if m.role == "assistant"][-1:]
    return "\n".join(str(t) for t in de_nea + del_lead)

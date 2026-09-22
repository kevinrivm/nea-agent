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
from app.prompt import build_system_prompt
from app.state import AppContext, InboundMessage, utcnow
from app.tools import ToolRuntime, tool_schemas

logger = logging.getLogger("nea.turn")

MAX_TOOL_ROUNDS = 5
# Mensajes que se traen, como mínimo, para contar el hilo del lead (el LLM ve
# menos). Si STALL_MAX_TURNS pide contar más de lo que cabe (un mensaje del
# lead por cada dos), se traen más; con los valores por defecto, 40 como siempre.
STALL_LOOKBACK = 40
CONTEXT_ATTEMPTS = 3  # el relay puede tardar un instante en aterrizar en el CRM
CONTEXT_PAUSE_SECONDS = 1.0  # entre esos intentos
# Cuando el CRM contesta 404 con el mensaje todavía en el relay, cuánto se le
# espera al relay (ya con su empujón) antes de volver a preguntar.
RELAY_ESPERA_SECONDS = 10.0
# Agotados los reintentos de un turno sin CRM (TURN_RETRY_DELAYS), cada cuánto
# se le vuelve a preguntar para pasarle la conversación a un humano, y hasta
# cuándo: el relay tampoco insiste más de 24 h, y pasado eso el CRM ya no va a
# tener el mensaje.
VIGILANCIA_SEGUNDOS = 60.0
VIGILANCIA_MAXIMA = timedelta(hours=24)

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


@dataclass
class _Turno:
    """Lo que un turno sabe de sí mismo, para la red de seguridad.

    `run_turn` lo va llenando mientras avanza; si revienta, `handle_flush` lo
    lee para saber a qué conversación del CRM avisarle y si ya se la había
    pasado a un humano.
    """

    # La conversación del CRM que este turno tomó: la que trajo el despacho
    # (cloud) o la del contexto, una vez pasados los gates.
    crm_conversation_id: str | None = None
    # El motivo del handoff que este turno ya registró, si registró uno.
    handoff: str | None = None


class TurnoSinCrm(Exception):
    """El turno no alcanzó al CRM al leer su contexto (gate 2).

    Todavía no se hizo nada que no se pueda repetir: ni se guardó el mensaje
    del lead, ni se pensó, ni se le escribió. Por eso, y solo por eso, la
    ráfaga se puede volver a intentar entera sin contestar dos veces.
    """


@dataclass
class _Pendiente:
    """Una ráfaga que no alcanzó al CRM y espera su reintento.

    Hay a lo más una por conversación (`ctx.turnos_pendientes`): el mensaje
    que llega mientras tanto se la lleva consigo y su turno contesta todo a
    la vez, en vez de que al volver el CRM salgan dos respuestas encimadas.
    """

    items: list[Any]
    fallas: int  # turnos de esta ráfaga que no alcanzaron al CRM
    crm_conversation_id: str | None = None
    tarea: "asyncio.Task[None] | None" = None
    # Se agotaron los reintentos: la tarea ya no reintenta, espera a que el
    # CRM conteste para pasarle la conversación a un humano.
    rendida: bool = False
    # Lo pone `reanudar_pendientes` cuando el CRM vuelve: la espera se corta
    # y el intento (o la vigilia) va ya, sin cancelar nada a medio turno.
    despertar: asyncio.Event = field(default_factory=asyncio.Event)


def _clave(ctx: AppContext, identity: str) -> tuple[str, str]:
    # La conversación es de (organización, identidad): con varios negocios,
    # la ráfaga de uno no se puede fusionar con la de otro.
    return ((ctx.organizacion or ("", ""))[0], identity)


def _marcas(items: list[Any]) -> list[str]:
    return [str(w) for w in (getattr(m, "wa_message_id", None) for m in items) if w]


def _fusionar(viejos: list[Any], nuevos: list[Any]) -> list[Any]:
    """La ráfaga que esperaba y la nueva, en orden y sin repetir un wamid."""
    vistos: set[str] = set()
    juntos: list[Any] = []
    for m in [*viejos, *nuevos]:
        wamid = getattr(m, "wa_message_id", None)
        if wamid:
            if wamid in vistos:
                continue
            vistos.add(wamid)
        juntos.append(m)
    return juntos


async def handle_flush(
    ctx: AppContext,
    identity: str,
    items: list[Any],
    crm_conversation_id: str | None = None,
    *,
    reintento: _Pendiente | None = None,
) -> None:
    """Callback del coalescer (y del despacho en cloud) — nunca propaga excepciones.

    Un turno que revienta por algo inesperado —un fallo de código, la base
    caída a medio turno, un CRM que contesta algo que no es JSON— dejaba al
    lead sin respuesta y sin nadie que lo viera: solo quedaba el log. Ahora,
    además del log (con los wamids de la ráfaga, para encontrarla), se
    registra un handoff `error` en el CRM si se sabe de qué conversación era,
    para que un humano la atienda. El handoff va por `ctx.crm`, el cliente
    del turno: `/api/bot` en el modo de siempre y, en cloud, `/api/brains`
    con la credencial de ESA organización — igual que el handoff normal.

    `crm_conversation_id` lo pasa el despacho en cloud, donde el CRM ya dijo
    qué conversación es; en el modo de siempre se sabe hasta que el turno
    pasa los gates con el contexto del CRM.

    Un turno que no alcanzó al CRM (red, 5xx, o un 404 mientras el relay aún
    no le entrega el mensaje) ya no acaba en silencio: la MISMA ráfaga se
    reprograma con esperas crecientes (TURN_RETRY_DELAYS) sin volver a pasar
    por el dedup del webhook, y agotadas las esperas la conversación pasa a
    un humano en cuanto el CRM conteste. `reintento` lo pone ese reintento:
    si al tomar el candado un mensaje nuevo ya se llevó su ráfaga, no hay
    nada que hacer.
    """
    turno = _Turno(crm_conversation_id=crm_conversation_id or None)
    clave = _clave(ctx, identity)
    try:
        async with conversation_lock(ctx, identity):
            pendiente = ctx.turnos_pendientes.get(clave)
            if reintento is not None and pendiente is not reintento:
                return  # un mensaje nuevo ya se llevó esta ráfaga
            fallas = 0
            if pendiente is not None:
                del ctx.turnos_pendientes[clave]
                tarea = pendiente.tarea
                if tarea is not None and tarea is not asyncio.current_task():
                    tarea.cancel()
                if reintento is None:
                    logger.info(
                        "turno de %s: el mensaje nuevo se lleva la ráfaga que "
                        "esperaba al CRM (wamids: %s)",
                        identity,
                        ", ".join(_marcas(pendiente.items)) or "ninguno",
                    )
                items = _fusionar(pendiente.items, items)
                turno.crm_conversation_id = (
                    turno.crm_conversation_id or pendiente.crm_conversation_id
                )
                # Rendida, el lead que vuelve a escribir abre una tanda nueva
                # de reintentos: sigue ahí, esperando respuesta.
                fallas = 0 if pendiente.rendida else pendiente.fallas
            try:
                await run_turn(ctx, identity, items, turno)
            except TurnoSinCrm as exc:
                _reprogramar(ctx, identity, items, fallas + 1, turno, str(exc))
    except Exception:
        wamids = [
            str(w) for w in (getattr(m, "wa_message_id", None) for m in items) if w
        ]
        logger.exception(
            "turno de %s reventó (wamids: %s) — silencio",
            identity,
            ", ".join(wamids) or "ninguno",
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


def _reprogramar(
    ctx: AppContext,
    identity: str,
    items: list[Any],
    fallas: int,
    turno: _Turno,
    motivo: str,
) -> None:
    """Deja la ráfaga esperando al CRM: otro intento o, agotados, la vigilia."""
    esperas = ctx.settings.turn_retry_schedule
    pendiente = _Pendiente(
        items=list(items), fallas=fallas, crm_conversation_id=turno.crm_conversation_id
    )
    ctx.turnos_pendientes[_clave(ctx, identity)] = pendiente
    marcas = ", ".join(_marcas(items)) or "ninguno"
    if fallas <= len(esperas):
        espera = esperas[fallas - 1]
        logger.warning(
            "turno de %s: el CRM no contestó (%s) — reintento %d de %d en %.0f s "
            "(wamids: %s)",
            identity,
            motivo,
            fallas,
            len(esperas),
            espera,
            marcas,
        )
        pendiente.tarea = asyncio.create_task(
            _reintentar(ctx, identity, pendiente, espera), name=f"reintento-{identity}"
        )
        return
    pendiente.rendida = True
    logger.error(
        "turno de %s: el CRM no contestó tras %d reintentos (%s) — sin respuesta; "
        "la conversación pasa a un humano en cuanto el CRM conteste (wamids: %s)",
        identity,
        len(esperas),
        motivo,
        marcas,
    )
    pendiente.tarea = asyncio.create_task(
        _vigilar(ctx, identity, pendiente), name=f"vigilia-{identity}"
    )


async def _dormir(pendiente: _Pendiente, segundos: float) -> None:
    """Espera `segundos`, o menos si el CRM vuelve antes."""
    try:
        await asyncio.wait_for(pendiente.despertar.wait(), timeout=segundos)
    except asyncio.TimeoutError:
        pass
    pendiente.despertar.clear()


def reanudar_pendientes(ctx: AppContext) -> int:
    """El CRM volvió: lo que esperaba su reintento se intenta ya.

    La llama el relay cuando entrega algo que antes no pudo. Sin esto, con
    el CRM de vuelta, la respuesta esperaba el resto de su espera (hasta
    5 min con los valores por defecto). Devuelve cuántas despertó.
    """
    for pendiente in ctx.turnos_pendientes.values():
        pendiente.despertar.set()
    return len(ctx.turnos_pendientes)


async def _reintentar(
    ctx: AppContext, identity: str, pendiente: _Pendiente, espera: float
) -> None:
    await _dormir(pendiente, espera)
    await handle_flush(
        ctx, identity, [], pendiente.crm_conversation_id, reintento=pendiente
    )


async def _vigilar(ctx: AppContext, identity: str, pendiente: _Pendiente) -> None:
    """Agotados los reintentos: en cuanto el CRM conteste, a un humano.

    Sin esto, al volver el CRM el mensaje aparecía en la bandeja (el relay lo
    entrega) con la IA encendida y nadie le contestaba. Best-effort: nunca
    lanza, y un mensaje nuevo del lead la cancela (su turno se lleva la ráfaga).
    """
    clave = _clave(ctx, identity)
    marcas = _marcas(pendiente.items)
    limite = utcnow() + VIGILANCIA_MAXIMA
    try:
        while utcnow() < limite:
            await _dormir(pendiente, VIGILANCIA_SEGUNDOS)
            if ctx.turnos_pendientes.get(clave) is not pendiente:
                return
            try:
                context = await ctx.crm.get_context(identity)
            except CrmError:
                continue  # sigue sin contestar
            if context is None:
                if await _relay_lo_tiene(ctx, marcas):
                    await _empujar_relay(ctx)
                    continue  # el CRM volvió, pero aún no recibe el mensaje
                logger.warning(
                    "turno de %s: el CRM volvió y no conoce la conversación — "
                    "sin handoff",
                    identity,
                )
                break
            info = context.get("conversation") or {}
            conv_id = info.get("id") or pendiente.crm_conversation_id
            async with conversation_lock(ctx, identity):
                if ctx.turnos_pendientes.get(clave) is not pendiente:
                    return  # un mensaje nuevo se la llevó mientras tanto
                del ctx.turnos_pendientes[clave]
                if not conv_id:
                    logger.warning(
                        "turno de %s: contexto sin conversationId — sin handoff",
                        identity,
                    )
                    return
                if not info.get("aiEnabled", False):
                    logger.info(
                        "turno de %s: ya la atiende una persona — sin handoff",
                        identity,
                    )
                    return
                await ctx.crm.post_handoff(str(conv_id), "error")
                logger.info(
                    "turno de %s: el CRM volvió tarde para contestar — handoff "
                    "error registrado en %s",
                    identity,
                    conv_id,
                )
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "turno de %s: no pude pasar la conversación a un humano (%s)",
            identity,
            exc,
        )
    if ctx.turnos_pendientes.get(clave) is pendiente:
        del ctx.turnos_pendientes[clave]
    logger.error(
        "turno de %s: se deja de esperar al CRM (wamids: %s)",
        identity,
        ", ".join(marcas) or "ninguno",
    )


async def cancelar_pendientes(ctx: AppContext) -> None:
    """Al apagar: los reintentos viven en memoria y se van con el proceso.

    El mensaje no se pierde —el relay lo guarda en Postgres y el CRM lo enseña
    en la bandeja cuando vuelve—; lo que se pierde es la respuesta de Nea.
    """
    pendientes = list(ctx.turnos_pendientes.values())
    ctx.turnos_pendientes.clear()
    tareas = [p.tarea for p in pendientes if p.tarea is not None and not p.tarea.done()]
    for tarea in tareas:
        tarea.cancel()
    if tareas:
        await asyncio.gather(*tareas, return_exceptions=True)
    if pendientes:
        logger.warning(
            "apagando con %d ráfaga(s) esperando al CRM — quedan sin respuesta",
            len(pendientes),
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
    allowed = settings.allowed_identities
    if allowed and canonical_identity(identity) not in allowed:
        logger.info(
            "allowlist: %s fuera de ALLOWED_WA_IDS — relay sí, respuesta no", identity
        )
        return

    # La conversación es de una ORGANIZACIÓN, no solo de una identidad. Con
    # una Nea de un solo negocio la tupla es vacía y todo queda igual que
    # siempre; con varias, es lo que impide que el mismo teléfono escribiendo
    # a dos negocios comparta historial. Ver migrations/004_multiorg.sql.
    org_id, org_slug = ctx.organizacion or ("", "")
    conv = await ctx.store.get_or_create_conversation(identity, org_id, org_slug)

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
    # (handle_flush). Solo el 404 de verdad —no conoce la identidad y el
    # relay ya no tiene nada que entregarle— termina en silencio.
    try:
        context = await _fetch_context(ctx, identity, _marcas(inbound))
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

    # --- Armar mensajes para el LLM ---------------------------------------
    # ¿El CRM agenda HOY? La respuesta caduca (app/agenda.py): si venció, este
    # turno la vuelve a pedir, con timeout corto. Así encender AGENDA en el
    # CRM llega sin reiniciar Nea, y un 404 de la bandera no apaga para siempre.
    ctx.agenda_enabled = await agenda_vigente(ctx)
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
        recordatorios=bool(getattr(ctx.crm, "supports_agenda_v2", False)),
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
    runtime = ToolRuntime(ctx, conv, str(crm_conv_id), profile=profile)
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
            cerrada_en.astimezone(_agent_tz(settings)).isoformat(timespec="minutes"),
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


async def _fetch_context(
    ctx: AppContext, identity: str, marcas: list[str] | None = None
) -> dict[str, Any] | None:
    """El contexto del CRM, o None si el CRM dice que no conoce la identidad.

    Lanza `CrmUnreachable` si en el último intento el CRM no contestó (red,
    timeout, 5xx), y también si contestó 404 mientras el relay aún guarda el
    payload de esta ráfaga (`marcas`, sus wamids): ese 404 no dice «no
    existe», dice «todavía no me llega». Al verlo se le da un empujón al
    relay para que entregue ya, sin esperar su backoff.
    """
    caida: CrmUnreachable | None = None
    no_existe = False
    for attempt in range(CONTEXT_ATTEMPTS):
        caida, no_existe = None, False
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
            no_existe = context is None
        if context is not None:
            return context
        if no_existe and await _relay_lo_tiene(ctx, marcas):
            await _empujar_relay(ctx)
        if attempt < CONTEXT_ATTEMPTS - 1:
            await asyncio.sleep(CONTEXT_PAUSE_SECONDS)  # que el relay aterrice
    if caida is not None:
        raise caida
    if no_existe and await _relay_lo_tiene(ctx, marcas):
        # El CRM contesta y el relay ya tiene su empujón: se le da un momento
        # para entregar y se pregunta una vez más, en vez de dejar al lead
        # esperando la siguiente vuelta de reintentos.
        if await _esperar_al_relay(ctx, marcas):
            try:
                context = await ctx.crm.get_context(identity)
            except CrmError as exc:
                logger.warning("context de %s tras el relay: %s", identity, exc)
                context = None
            if context is not None:
                return context
        raise CrmUnreachable(
            "el CRM contestó 404 y el relay aún no le entrega el mensaje"
        )
    return None


async def _esperar_al_relay(ctx: AppContext, marcas: list[str] | None) -> bool:
    """True si el relay entregó la ráfaga dentro de RELAY_ESPERA_SECONDS."""
    fin = asyncio.get_running_loop().time() + RELAY_ESPERA_SECONDS
    while asyncio.get_running_loop().time() < fin:
        await asyncio.sleep(min(0.5, RELAY_ESPERA_SECONDS))
        if not await _relay_lo_tiene(ctx, marcas):
            return True
    return False


async def _relay_lo_tiene(ctx: AppContext, marcas: list[str] | None) -> bool:
    """¿El relay todavía guarda, sin entregar, el payload de esta ráfaga?"""
    if not marcas or ctx.settings.cloud_mode:
        return False  # en cloud no hay relay: el CRM mismo despachó el mensaje
    try:
        return bool(await ctx.store.relay_pendiente_con(marcas))
    except Exception as exc:
        logger.warning("no pude mirar la cola del relay (%s)", exc)
        return False


async def _empujar_relay(ctx: AppContext) -> None:
    """El CRM ya contesta: lo que el relay tenía en espera sale ahora."""
    try:
        await ctx.store.adelantar_relays(utcnow())
    except Exception as exc:
        logger.warning("no pude adelantar la cola del relay (%s)", exc)
    ctx.relay_wake.set()


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

"""Reintento diferido de envíos: lo que el turno no pudo entregar, sale después.

Loop asyncio cada 30 s sobre `pending_send` (encolado por app/turn.py cuando el
envío agota sus reintentos). Backoff exponencial con tope de 15 min; se
abandona si el CRM lo rechaza con 409 (ai_paused / window_closed) o al agotar
24 h — en ese caso se registra handoff `error` para que un humano vea la
conversación en el CRM (incidente 2026-08-03: antes la respuesta se descartaba
en silencio y el lead no recibía nada).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.crm import CrmConflict, CrmError
from app.multiorg import ctx_de_organizacion
from app.state import AppContext, PendingSend, utcnow

logger = logging.getLogger("nea.sender")


class SenderWorker:
    INTERVAL = 30.0
    MAX_AGE = timedelta(hours=24)  # la ventana de WhatsApp ya habrá cerrado
    BACKOFF_BASE = 30.0
    BACKOFF_CAP = 900.0  # 15 min entre intentos, máximo

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            try:
                await self.tick()
            except Exception:
                logger.exception("sender: fallo en el barrido")

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        for item in await self._ctx.store.due_pending_sends(now):
            # Con quién hay que hablar para ESTA fila. En una Nea de un solo
            # negocio es siempre el mismo; en una multi-organización, el de
            # la organización que dejó el envío pendiente.
            ctx = ctx_de_organizacion(
                self._ctx, item.organization_id, item.organization_slug
            )
            if ctx is None:
                logger.error(
                    "sender: pending %d sin organización conocida — no puedo "
                    "entregarlo sin arriesgarme a escribirle a otro negocio",
                    item.id,
                )
                await self._ctx.store.mark_pending_send_abandoned(item.id)
                continue
            if now - item.created_at > self.MAX_AGE:
                logger.error(
                    "sender: pending %d agotó las 24 h sin entregar — abandonado + handoff",
                    item.id,
                )
                await self._ctx.store.mark_pending_send_abandoned(item.id)
                await self._handoff(ctx, item.crm_conversation_id)
                continue
            await self._attempt(ctx, item, now)

    async def _attempt(
        self, ctx: AppContext, item: PendingSend, now: datetime
    ) -> None:
        # Qué despacho estaba contestando. El CRM lo exige para responder por
        # el cerebro, y aquí el turno que lo sabía ya no existe: sale de la
        # fila. Sin esto el reintento diferido se rechazaba con 422 hasta
        # agotar las 24 h — la cola entera existía para nada.
        registrar_despacho = getattr(ctx.crm, "registrar_despacho", None)
        if callable(registrar_despacho):
            registrar_despacho(item.crm_conversation_id, item.dispatch_id)
        try:
            await ctx.crm.send_message(item.crm_conversation_id, item.content)
        except CrmConflict as exc:
            # ai_paused / window_closed: ya no procede — silencio respetuoso.
            logger.info(
                "sender: pending %d rechazado por el CRM (%s) — abandonado",
                item.id,
                exc.code,
            )
            await ctx.store.mark_pending_send_abandoned(item.id)
            return
        except CrmError as exc:
            attempts = item.attempts + 1
            delay = min(self.BACKOFF_BASE * (2.0**item.attempts), self.BACKOFF_CAP)
            logger.warning(
                "sender: pending %d falló (intento %d): %s — reintento en %.0f s",
                item.id,
                attempts,
                exc,
                delay,
            )
            await ctx.store.reschedule_pending_send(
                item.id, attempts, now + timedelta(seconds=delay)
            )
            return
        await ctx.store.mark_pending_send_delivered(item.id)
        # Al historial local: Nea debe recordar lo que (por fin) dijo.
        await ctx.store.add_message(item.conversation_id, "assistant", item.content)
        logger.info("sender: pending %d entregado", item.id)

    async def _handoff(self, ctx: AppContext, crm_conv_id: str) -> None:
        try:
            await ctx.crm.post_handoff(crm_conv_id, "error")
        except CrmError as exc:
            logger.error("sender: no pude registrar el handoff tras abandono: %s", exc)

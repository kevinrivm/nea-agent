"""One bounded consumer per process; PostgreSQL serializes each organization."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.multiorg import organizacion_del_despacho

logger = logging.getLogger("nea.dispatch.worker")


async def handoff(ctx: Any, payload: dict[str, Any]) -> None:
    client = ctx.crm
    if ctx.settings.multi_org:
        org = organizacion_del_despacho(payload)
        if org is None or ctx.registro is None:
            raise ValueError("missing dispatch organization")
        client = ctx.registro.cliente(*org)
    await client.post_handoff(payload["conversation"]["id"], "error")


async def drain_one(ctx: Any) -> bool:
    from app.dispatch import EVENTO_OLVIDO, _procesar, olvidar
    async with ctx.store.claim_dispatch() as job:
        if job is None:
            return False

        async def execute() -> None:
            if job.payload.get("type") == EVENTO_OLVIDO:
                # Not a turn: nothing to hand off, and replaying it is safe.
                await olvidar(ctx, job.payload)
                return
            expires = job.payload.get("expiresAt")
            expired = bool(expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc))
            if job.recovery or expired:
                await handoff(ctx, job.payload)
            else:
                try:
                    await _procesar(ctx, job.payload, durable=True)
                except Exception:
                    logger.exception("dispatch failed; handing off without replay")
                    await handoff(ctx, job.payload)

        task = asyncio.create_task(execute())
        try:
            async with asyncio.timeout(240):
                while not task.done():
                    await asyncio.wait({task}, timeout=20)
                    if not task.done():
                        await job.heartbeat()
                await task
            await job.finish()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True


async def run(ctx: Any) -> None:
    while True:
        try:
            if await drain_one(ctx):
                continue
        except Exception:
            logger.exception("dispatch remains durable for recovery")
        ctx.dispatch_wake.clear()
        try:
            await asyncio.wait_for(ctx.dispatch_wake.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass

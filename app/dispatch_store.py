"""Durable acceptance; interrupted started turns are never replayed.

A session advisory lock covers preparation, effects and terminal state for an
organization. It also protects the per-org cached CRM envelope across replicas.
The lock is released by PostgreSQL if the process dies. A lease adds a grace
period before recovery; owner fencing prevents a late completion overwriting it.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4


@dataclass
class DispatchJob:
    payload: dict[str, Any]
    recovery: bool
    finish: Callable[[], Awaitable[None]]
    heartbeat: Callable[[], Awaitable[None]]


class PgDispatchStore:
    pool: Any

    async def enqueue_dispatch(self, org: str, payload: dict[str, Any]) -> None:
        await self.pool.execute("""INSERT INTO dispatch_inbox
          (organization_id,dispatch_id,conversation_id,payload) VALUES ($1,$2,$3,$4::jsonb)
          ON CONFLICT DO NOTHING""", org, payload["dispatchId"], payload["conversation"]["id"], json.dumps(payload))

    async def forget_dispatches(self, org: str, conversation_id: str, keep: str) -> None:
        """The payload column holds the lead's messages verbatim. `keep` is the
        row being processed right now: finish() must still find it."""
        await self.pool.execute("""DELETE FROM dispatch_inbox
          WHERE organization_id=$1 AND conversation_id=$2 AND dispatch_id<>$3""", org, conversation_id, keep)

    @asynccontextmanager
    async def claim_dispatch(self) -> AsyncIterator[DispatchJob | None]:
        async with self.pool.acquire() as conn:
            candidates = await conn.fetch("""SELECT DISTINCT organization_id FROM dispatch_inbox
              WHERE state<>'done' AND available_at<=now() LIMIT 50""")
            for candidate in candidates:
                org = candidate["organization_id"]
                key = "nea:dispatch:" + org
                locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", key)
                if not locked:
                    continue
                try:
                    async with conn.transaction():
                        if await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM dispatch_inbox
                          WHERE organization_id=$1 AND state='started' AND available_at>now())""", org):
                            continue
                        row = await conn.fetchrow("""SELECT * FROM dispatch_inbox WHERE organization_id=$1
                          AND state<>'done' AND available_at<=now() ORDER BY created_at
                          FOR UPDATE SKIP LOCKED LIMIT 1""", org)
                        if row is None:
                            continue
                        owner = uuid4().hex
                        dispatch_id = row["dispatch_id"]
                        await conn.execute("""UPDATE dispatch_inbox SET state='started',owner=$3,
                          available_at=now()+interval '3 minutes',updated_at=now()
                          WHERE organization_id=$1 AND dispatch_id=$2""", org, dispatch_id, owner)

                    async def finish() -> None:
                        result = await conn.execute("""UPDATE dispatch_inbox SET state='done',updated_at=now()
                          WHERE organization_id=$1 AND dispatch_id=$2 AND owner=$3 AND state='started'""", org, dispatch_id, owner)
                        if result != "UPDATE 1":
                            raise RuntimeError("dispatch ownership lost")

                    async def heartbeat() -> None:
                        result = await conn.execute("""UPDATE dispatch_inbox SET available_at=now()+interval '3 minutes',updated_at=now()
                          WHERE organization_id=$1 AND dispatch_id=$2 AND owner=$3 AND state='started'""", org, dispatch_id, owner)
                        if result != "UPDATE 1":
                            raise RuntimeError("dispatch ownership lost")

                    yield DispatchJob(json.loads(row["payload"]), row["state"] != "queued", finish, heartbeat)
                    return
                finally:
                    if not conn.is_closed():
                        await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)
            yield None


class MemoryDispatchStore:
    def _inbox(self) -> dict[Any, Any]:
        if not hasattr(self, "dispatch_inbox"):
            self.dispatch_inbox: dict[Any, Any] = {}
            self.dispatch_lock = asyncio.Lock()
        return self.dispatch_inbox

    async def enqueue_dispatch(self, org: str, payload: dict[str, Any]) -> None:
        self._inbox().setdefault((org, payload["dispatchId"]), {
            "payload": json.loads(json.dumps(payload)), "state": "queued", "available_at": datetime.now(timezone.utc),
        })

    async def forget_dispatches(self, org: str, conversation_id: str, keep: str) -> None:
        inbox = self._inbox()
        for key in [k for k, row in inbox.items() if k[0] == org and k[1] != keep
                    and row["payload"]["conversation"]["id"] == conversation_id]:
            del inbox[key]

    @asynccontextmanager
    async def claim_dispatch(self) -> AsyncIterator[DispatchJob | None]:
        inbox = self._inbox()
        async with self.dispatch_lock:
            for row in inbox.values():
                if row["state"] == "done" or row["available_at"] > datetime.now(timezone.utc):
                    continue
                recovery = row["state"] != "queued"
                row["state"] = "started"
                row["available_at"] = datetime.now(timezone.utc) + timedelta(minutes=3)

                async def finish() -> None:
                    row["state"] = "done"

                async def heartbeat() -> None:
                    row["available_at"] = datetime.now(timezone.utc) + timedelta(minutes=3)

                yield DispatchJob(row["payload"], recovery, finish, heartbeat)
                return
            yield None

"""Relay del webhook al CRM: cola persistente en Postgres + backoff exponencial.

Se reenvía el BODY CRUDO (bytes exactos) con el header `x-hub-signature-256`
original, para que la ingesta idempotente del CRM verifique la firma de Meta
tal cual. Un hipo del CRM no pierde el mensaje: reintentos hasta entregar o
agotar 24 h desde el encolado.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx

from app.state import RelayItem, Store, utcnow

logger = logging.getLogger("nea.relay")


class RelayWorker:
    MAX_AGE = timedelta(hours=24)  # tope duro de reintentos
    # Tope de la espera entre intentos (RELAY_BACKOFF_CAP_SECONDS). Era de
    # 15 min: tras una caída larga del CRM, el mensaje llegaba a su bandeja
    # hasta 15 min DESPUÉS de que el CRM ya había vuelto. Con 60 s, la cola se
    # vacía como mucho un minuto después, y un intento por minuto por fila no
    # le pesa a nadie.
    BACKOFF_CAP = 60.0
    IDLE_SCAN = 5.0  # barrido periódico aunque nadie despierte al worker

    def __init__(
        self,
        store: Store,
        webhook_url: str,
        wake: asyncio.Event,
        http: httpx.AsyncClient | None = None,
        backoff_cap: float | None = None,
        al_volver: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store
        self._url = webhook_url
        self._wake = wake
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._cap = max(1.0, float(backoff_cap)) if backoff_cap else self.BACKOFF_CAP
        # Se llama al entregar algo que antes falló: el CRM volvió. main.py la
        # conecta con los turnos que esperaban al CRM (app/turn.py).
        self._al_volver = al_volver

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.IDLE_SCAN)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.process_due()
            except Exception:
                logger.exception("relay: fallo procesando la cola")

    async def process_due(self, now: datetime | None = None) -> None:
        """Un barrido: TODO lo que vence a `now`, no solo la primera tanda.

        `due_relays` devuelve de a 50 (los más viejos primero). Con un solo
        lote por barrido, una cola atrasada —la que dejó el relay roto de
        95549c8: cada webhook de Meta desde el 31-ago— se vaciaba a 50 filas
        cada 5 s, y los mensajes nuevos esperaban detrás de miles de filas
        viejas (que solo se abandonan). Ahora el barrido sigue pidiendo lotes
        hasta que no quede nada vencido que no haya visto ya: lo que falla se
        reprograma al futuro y no vuelve en este mismo barrido.
        """
        now = now or utcnow()
        vistos: set[int] = set()
        while True:
            lote = [i for i in await self._store.due_relays(now) if i.id not in vistos]
            if not lote:
                return
            for item in lote:
                vistos.add(item.id)
                await self._process_item(item, now)

    async def _process_item(self, item: RelayItem, now: datetime) -> None:
        if now - item.created_at > self.MAX_AGE:
            logger.error(
                "relay: item %d agotó las 24 h sin entregar — abandonado", item.id
            )
            await self._store.mark_relay_abandoned(item.id)
            return
        if await self._deliver(item):
            await self._store.mark_relay_delivered(item.id)
            if item.attempts > 0 and self._al_volver is not None:
                try:
                    self._al_volver()
                except Exception:
                    logger.exception("relay: el aviso de que el CRM volvió falló")
        else:
            attempts = item.attempts + 1
            delay = min(2.0**attempts, self._cap)
            logger.warning(
                "relay: item %d falló (intento %d), reintento en %.0f s",
                item.id,
                attempts,
                delay,
            )
            await self._store.reschedule_relay(
                item.id, attempts, now + timedelta(seconds=delay)
            )

    async def _deliver(self, item: RelayItem) -> bool:
        headers = {"content-type": "application/json"}
        if item.signature:
            headers["x-hub-signature-256"] = item.signature
        try:
            resp = await self._http.post(self._url, content=item.body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("relay: error de red hacia el CRM: %s", exc)
            return False
        return 200 <= resp.status_code < 300

    async def aclose(self) -> None:
        await self._http.aclose()

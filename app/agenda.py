"""¿El CRM agenda? Una respuesta que caduca.

Vocero trae el motor de agenda detrás de la bandera `AGENDA`, apagada por
defecto. Antes Nea lo preguntaba UNA vez, al arrancar: encender la bandera en
el CRM exigía reiniciar Nea para que ofreciera citas, y en cuanto una
herramienta chocaba con el 404 la agenda quedaba apagada para todo el proceso,
hasta el siguiente reinicio.

Ahora la respuesta dura `AGENDA_PROBE_TTL_SECONDS` (60 por defecto). Cada
turno pregunta al empezar; si venció, se vuelve a sondear:

- a lo sumo UNA sonda por TTL, aunque lleguen varios turnos a la vez (los
  demás esperan esa misma y usan su resultado);
- con timeout corto: la sonda nunca retiene un turno más de `SONDA_TIMEOUT`;
- si la sonda no concluye (red caída, timeout, 5xx), se queda el último valor
  conocido — y no se vuelve a intentar hasta el siguiente TTL;
- el 404 de la bandera en una herramienta la apaga ya, pero solo hasta que
  venza el TTL: entonces se vuelve a preguntar.

Sigue valiendo lo de siempre: 404 vacío = apagada (ver `_agenda_apagada` en
app/crm.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger("nea.agenda")

# Segundos que una sonda puede retener al turno que la dispara.
SONDA_TIMEOUT = 3.0


class SondaDeAgenda:
    """El último «¿agenda?» conocido del CRM, re-preguntado al caducar."""

    def __init__(
        self,
        crm: Any,
        ttl: float,
        *,
        timeout: float = SONDA_TIMEOUT,
        inicial: bool = True,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self._crm = crm
        self._ttl = max(0.0, float(ttl))
        self._timeout = timeout
        self._reloj = reloj
        # Mientras no se sepa nada se asume que sí: equivocarse hacia el sí
        # cuesta un intento fallido; hacia el no apagaría una agenda que existe.
        self.valor = inicial
        self._vence: float | None = None  # None = nunca se ha preguntado
        self._candado = asyncio.Lock()
        # Sube con cada `marcar_apagada`: una sonda que salió ANTES de ese 404
        # trae una respuesta más vieja que él y no debe pisarlo.
        self._generacion = 0

    @property
    def ttl(self) -> float:
        return self._ttl

    async def vigente(self) -> bool:
        """El valor vigente; si caducó, sondea antes de contestar."""
        if not self._caducada():
            return self.valor
        async with self._candado:
            # Otro turno pudo sondear mientras este esperaba el candado.
            if self._caducada():
                await self._sondear()
        return self.valor

    def marcar_apagada(self) -> None:
        """Una herramienta recibió el 404 de la bandera: apagada hasta el TTL."""
        self._generacion += 1
        self.valor = False
        self._vence = self._reloj() + self._ttl

    def _caducada(self) -> bool:
        return self._vence is None or self._reloj() >= self._vence

    async def _sondear(self) -> None:
        generacion = self._generacion
        try:
            resultado = await asyncio.wait_for(
                self._preguntar(), timeout=self._timeout
            )
        except Exception as exc:  # timeout, o un cliente que no esperábamos
            logger.warning("agenda: la sonda falló (%r) — sigo con lo último", exc)
            resultado = None
        if generacion != self._generacion:
            return  # un 404 llegó mientras tanto: manda él, con su propio TTL
        # A lo sumo una sonda por TTL, concluya o no: un CRM caído no se
        # martillea en cada turno.
        self._vence = self._reloj() + self._ttl
        if resultado is None:
            logger.info(
                "agenda: la sonda no concluyó — sigo con %s",
                "disponible" if self.valor else "apagada",
            )
            return
        if resultado != self.valor:
            logger.info(
                "agenda del CRM: %s",
                "disponible" if resultado else "APAGADA — Nea no ofrecerá citas",
            )
        self.valor = bool(resultado)

    async def _preguntar(self) -> bool | None:
        sondear = getattr(self._crm, "sondear_agenda", None)
        if callable(sondear):
            return await sondear(timeout=self._timeout)
        # Un cliente que solo sabe la pregunta de siempre (pruebas viejas).
        return await self._crm.agenda_available()


async def agenda_vigente(ctx: Any) -> bool:
    """¿Agenda en ESTE turno? La sonda si hay; si no, lo fijado en el contexto.

    Las pruebas que arman el contexto a mano no traen sonda, y ahí manda
    `ctx.agenda_enabled` tal cual lo dejaron.
    """
    sonda = getattr(ctx, "agenda_sonda", None)
    if sonda is None:
        return bool(ctx.agenda_enabled)
    return await sonda.vigente()

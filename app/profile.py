"""Perfil del negocio: la capa que convierte a Nea en EL agente de UN negocio.

El chasis conductual (app/prompt.py) es genérico; todo lo que identifica al
negocio — nombre del agente, tono, instrucciones, reglas de escalado y
conocimiento aprobado — vive en un `BusinessProfile` que se resuelve así:

1. `GET /api/bot/profile` del CRM (agent profile + knowledge base que el
   dueño edita en la UI). Se cachea con TTL para que los cambios en la UI
   lleguen sin reiniciar el bot.
2. Fallback: un brief local en markdown (`BRIEF_PATH`) — para correr el bot
   sin CRM con perfil, o en desarrollo.
3. Último recurso: un perfil mínimo (el agente se presenta y agenda, pero
   avisa en logs que no tiene conocimiento del negocio).

Un fallo del CRM jamás tumba un turno: se sirve el último perfil conocido y,
si nunca hubo uno, el fallback.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.crm import CrmError

logger = logging.getLogger("nea.profile")

PROFILE_TTL_SECONDS = 300.0

# Render canónico del CRM para un knowledge base sin entradas (parte del
# contrato de GET /api/bot/profile): cuenta como "sin conocimiento".
EMPTY_KB_SENTINEL = "(knowledge base vacío)"


@dataclass(frozen=True)
class BusinessProfile:
    """Todo lo que el negocio configura del agente. Solo texto plano: el
    chasis decide dónde va cada pieza dentro del system prompt."""

    cloud: bool = False
    config_version: int = 1
    agent_name: str = "Nea"
    tone: str | None = None
    instructions: str | None = None
    escalation_rules: str | None = None
    greeting: str | None = None
    kb_text: str | None = None
    resources: list[dict[str, str]] = field(default_factory=list)

    @property
    def has_knowledge(self) -> bool:
        return bool((self.instructions or "").strip() or (self.kb_text or "").strip())


def profile_from_payload(payload: dict[str, Any], default_name: str) -> BusinessProfile:
    """Construye el perfil desde la respuesta del CRM, tolerante a ausencias."""
    prof = payload.get("profile") or {}
    resources_raw = payload.get("resources") or []
    kb_text = payload.get("kb") or None
    if isinstance(kb_text, str) and kb_text.strip() == EMPTY_KB_SENTINEL:
        kb_text = None
    resources = [
        {"label": str(r.get("label") or r.get("url") or ""), "url": str(r.get("url") or "")}
        for r in resources_raw
        if isinstance(r, dict) and r.get("url")
    ]
    return BusinessProfile(
        cloud=prof.get("cloud") is True,
        config_version=int(prof.get("configVersion") or 1),
        agent_name=str(prof.get("name") or default_name),
        tone=prof.get("tone") or None,
        instructions=prof.get("instructions") or None,
        escalation_rules=prof.get("escalationRules") or None,
        greeting=prof.get("greeting") or None,
        kb_text=kb_text,
        resources=resources,
    )


def profile_from_brief(path: Path, default_name: str) -> BusinessProfile | None:
    """Brief local: un markdown libre que se inyecta entero como instrucciones."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("no pude leer el brief local %s: %s", path, exc)
        return None
    if not text:
        return None
    return BusinessProfile(agent_name=default_name, instructions=text)


async def resolve_profile(ctx: Any) -> BusinessProfile:
    """Perfil vigente desde el AppContext; sin provider (tests) = mínimo."""
    if getattr(ctx, "profile", None) is None:
        return BusinessProfile(agent_name=getattr(ctx.settings, "agent_name", "Nea"))
    return await ctx.profile.get()


class ProfileProvider:
    """Resuelve el BusinessProfile vigente con cache TTL y degradación."""

    def __init__(
        self,
        crm: Any,
        *,
        default_name: str = "Nea",
        brief_path: str | None = None,
        ttl: float = PROFILE_TTL_SECONDS,
    ) -> None:
        self._crm = crm
        self._default_name = default_name
        self._brief_path = Path(brief_path) if brief_path else None
        self._ttl = ttl
        self._cached: BusinessProfile | None = None
        self._fetched_at: float = 0.0
        self._warned_minimal = False

    async def get(self) -> BusinessProfile:
        now = time.monotonic()
        if self._cached is not None and not self._cached.cloud and (now - self._fetched_at) < self._ttl:
            return self._cached

        payload = None
        try:
            payload = await self._crm.get_profile()
        except CrmError as exc:
            logger.warning("perfil: el CRM no respondió (%s) — uso el último conocido", exc)
        except AttributeError:
            payload = None  # cliente sin get_profile (tests viejos): fallback

        if payload is not None:
            self._cached = profile_from_payload(payload, self._default_name)
            self._fetched_at = now
            return self._cached

        # CRM sin perfil (404) o caído: último conocido > brief local > mínimo.
        if self._cached is not None:
            self._fetched_at = now  # no martillar al CRM caído en cada turno
            return self._cached
        if self._brief_path is not None:
            brief = profile_from_brief(self._brief_path, self._default_name)
            if brief is not None:
                self._cached = brief
                self._fetched_at = now
                logger.info("perfil: usando brief local %s", self._brief_path)
                return brief
        if not self._warned_minimal:
            logger.warning(
                "perfil: sin agent profile en el CRM ni BRIEF_PATH legible — "
                "el agente corre con el perfil mínimo (configura uno de los dos)"
            )
            self._warned_minimal = True
        self._cached = BusinessProfile(agent_name=self._default_name)
        self._fetched_at = now
        return self._cached

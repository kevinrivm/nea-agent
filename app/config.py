"""Configuración tipada del bot (pydantic-settings).

Todas las variables se documentan en `.env.example`. Los defaults permiten
importar el módulo sin entorno (los tests inyectan valores explícitos);
la validación de lo obligatorio ocurre al arranque real.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


def canonical_identity(wa_id: str) -> str:
    """Canonicaliza una identidad de WhatsApp para comparaciones.

    México: Meta a veces reporta `521XXXXXXXXXX` (13 dígitos con el "1" de
    móvil) y a veces `52XXXXXXXXXX` — son la misma persona. Los BSUID y otros
    identificadores pasan tal cual.
    """
    s = wa_id.strip()
    if s.startswith("521") and len(s) == 13 and s.isdigit():
        return "52" + s[3:]
    return s


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Webhook de Meta
    verify_token: str = ""
    meta_app_secret: str = ""  # vacío = no se verifica la firma (dev)

    # CRM (vocero-crm, bot gateway /api/bot/*)
    crm_base_url: str = "http://localhost:3000"
    crm_webhook_url: str = ""  # incluye el segmento del verify token del CRM
    crm_bot_api_key: str = ""

    # Perfil del negocio (capa de persona; ver app/profile.py)
    agent_name: str = "Nea"  # se usa si el CRM no define nombre
    agent_timezone: str = "America/Caracas"  # IANA; fechas del prompt
    brief_path: str = ""  # markdown local, fallback si el CRM no tiene perfil

    # LLM
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "whisper-1"  # notas de voz → texto
    history_window: int = 10

    # Guardarraíles y tiempos
    allowed_wa_ids: str = ""  # CSV; vacía = responde a todos (Constitución V)
    coalesce_seconds: float = 4.0
    followup_hours: float = 4.0
    # "Escribiendo…" casi inmediato al recibir un mensaje (antes del coalesce).
    typing_delay_seconds: float = 0.5

    # Infra
    database_url: str = ""
    port: int = 8000

    # Desarrollo: loguear el JSON crudo de mensajes no-texto entrantes para
    # capturar los formatos reales de Meta (spec 002). Apagar al terminar.
    capture_payloads: bool = False

    @property
    def allowed_identities(self) -> frozenset[str]:
        """Allowlist canonicalizada; vacía = sin restricción."""
        return frozenset(
            canonical_identity(part)
            for part in self.allowed_wa_ids.split(",")
            if part.strip()
        )

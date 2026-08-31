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
    agent_timezone: str = "America/Mexico_City"  # IANA; fechas del prompt
    brief_path: str = ""  # markdown local, fallback si el CRM no tiene perfil

    # LLM
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Proveedor del modelo. Vacío = OpenAI, como siempre.
    #
    # Cualquier API compatible con OpenAI vale: OpenRouter, un modelo propio,
    # lo que sea. Con OpenRouter va `https://openrouter.ai/api/v1` y el modelo
    # lleva su prefijo (`anthropic/claude-sonnet-4.5`).
    #
    # OJO CON LAS NOTAS DE VOZ: transcribir es una API propia de OpenAI, no
    # parte de lo compatible. Apuntando a otro proveedor, un audio del cliente
    # no se transcribe — Nea lo dice y pide que se lo escriban, en vez de
    # callarse. Si necesitas notas de voz, deja este campo vacío y usa OpenAI.
    openai_base_url: str = ""
    openai_transcribe_model: str = "whisper-1"  # notas de voz → texto
    history_window: int = 10

    # Guardarraíles y tiempos
    allowed_wa_ids: str = ""
    # Identidades que pueden usar el comando /reset. Va SEPARADA de
    # allowed_wa_ids a propósito: en producción esa lista va vacía (el agente
    # atiende a todos los leads), y cuando el /reset colgaba de ella el
    # comando quedaba muerto justo donde hace falta — para correr una ronda
    # de pruebas en vivo había que cerrarle la puerta a los leads reales.
    tester_wa_ids: str = ""  # CSV; vacía = responde a todos (Constitución V)
    coalesce_seconds: float = 4.0
    followup_hours: float = 4.0
    # "Escribiendo…" casi inmediato al recibir un mensaje (antes del coalesce).
    typing_delay_seconds: float = 0.5

    # ── Modo cloud (Vocero multitenant) ───────────────────────────────
    #
    # Vacío = comportamiento de SIEMPRE: Meta manda el webhook a Nea, Nea lo
    # relaya al CRM y habla con `/api/bot/*`. Ninguna instalación existente
    # cambia por añadir esto, y ninguna variable nueva es obligatoria.
    #
    # `cloud` = Nea vive detrás de un Vocero multitenant: el CRM ya recibió el
    # mensaje y se lo DESPACHA a Nea firmado, y Nea contesta por
    # `/api/brains/*`. Una sola Nea sirve a muchos negocios, y cada uno trae su
    # personalidad desde SU CRM.
    vocero_mode: str = ""

    # Secreto del cerebro, el que genera el CRM en Ajustes → Cerebro. Firma lo
    # que llega y autentica lo que sale: el mismo secreto en las dos
    # direcciones, como en el contrato.
    crm_brain_secret: str = ""

    # Slug de la organización. Solo se usa para acompañar al secreto; el CRM
    # verifica que coincidan, así que no sirve de nada acertar uno sin el otro.
    crm_organization: str = ""

    # Infra
    database_url: str = ""
    port: int = 8000

    # Desarrollo: loguear el JSON crudo de mensajes no-texto entrantes para
    # capturar los formatos reales de Meta (spec 002). Apagar al terminar.
    capture_payloads: bool = False

    @property
    def cloud_mode(self) -> bool:
        """¿Nea corre detrás de un Vocero multitenant?

        Se compara en minúsculas y sin espacios para que un `Cloud ` copiado
        de una consola no deje a nadie preguntándose por qué no arrancó en el
        modo que pidió.
        """
        return self.vocero_mode.strip().lower() == "cloud"

    @staticmethod
    def _identities(csv: str) -> frozenset[str]:
        return frozenset(
            canonical_identity(part) for part in csv.split(",") if part.strip()
        )

    @property
    def allowed_identities(self) -> frozenset[str]:
        """Allowlist canonicalizada; vacía = sin restricción."""
        return self._identities(self.allowed_wa_ids)

    @property
    def tester_identities(self) -> frozenset[str]:
        """Quién puede correr /reset. Vacía = comando apagado."""
        return self._identities(self.tester_wa_ids)

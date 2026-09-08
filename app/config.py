"""Configuración tipada del bot (pydantic-settings).

Todas las variables se documentan en `.env.example`. Los defaults permiten
importar el módulo sin entorno (los tests inyectan valores explícitos);
la validación de lo obligatorio ocurre al arranque real.
"""
from __future__ import annotations

from pydantic import AliasChoices, Field
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
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Los campos se pueden dar por su nombre además de por su alias: es lo
        # que deja construir Settings(llm_api_key=…) en las pruebas.
        populate_by_name=True,
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
    # El proveedor del modelo no tiene por qué ser OpenAI, así que las
    # variables no llevan su nombre: llamarlas OPENAI_* apuntando a OpenRouter
    # es decir una cosa y hacer otra.
    #
    # Los nombres viejos SIGUEN funcionando. Renombrar a secas habría roto a
    # toda instalación existente en su siguiente redespliegue, y una variable
    # que deja de leerse no avisa: el bot arranca y se queda mudo.
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY")
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("LLM_MODEL", "OPENAI_MODEL"),
    )
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
    llm_base_url: str = Field(
        default="", validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL")
    )
    # Notas de voz → texto. Con OpenAI es whisper. Con un proveedor propio NO
    # hay endpoint de transcripción: hay que poner aquí un modelo que ACEPTE
    # AUDIO, y el audio viaja dentro del chat.
    #
    # No tiene por qué ser el mismo que conversa: hoy los GLM, por ejemplo, no
    # oyen. Uno conversa y otro escucha, con la misma clave.
    llm_transcribe_model: str = Field(
        default="whisper-1",
        validation_alias=AliasChoices(
            "LLM_TRANSCRIBE_MODEL", "OPENAI_TRANSCRIBE_MODEL"
        ),
    )

    # Esfuerzo de razonamiento del modelo. "minimal" lo baja al mínimo.
    #
    # El razonamiento se cobra en segundos que el cliente pasa mirando
    # "escribiendo…". Medido en huaraches-nea el 7-sep contra GLM 5.3 Flash:
    # con razonamiento, 150 tokens PENSANDO por cada 146 caracteres de
    # respuesta y 4.4 s; con "minimal", 2.0 s y cero. El mismo modelo y la
    # misma respuesta.
    #
    # Vacía = no se manda el parámetro y decide el proveedor. Un modelo que no
    # entienda `reasoning` lo ignora: va por `extra_body`, fuera del contrato
    # de OpenAI. Y si alguno se quejara, la llamada se reintenta sin él (ver
    # `llm.py`) — bajar el tiempo no puede costar una respuesta.
    llm_reasoning_effort: str = Field(
        default="minimal", validation_alias=AliasChoices("LLM_REASONING_EFFORT")
    )

    # A qué proveedor de OpenRouter ir. "throughput" = al más rápido.
    #
    # La misma llamada tardaba 5.4 s o 19.6 s según a quién la ruteara: la
    # varianza entre proveedores era MAYOR que cualquier optimización del
    # prompt. Vacía = decide OpenRouter, que ordena por precio.
    llm_provider_sort: str = Field(
        default="throughput", validation_alias=AliasChoices("LLM_PROVIDER_SORT")
    )

    # NO hay tope de tokens de salida, y es una decisión, no un olvido.
    #
    # En huaraches se puso uno el 8-sep con un banco que prometía partir a la
    # mitad el peor caso. En producción hizo lo contrario: el modelo gastaba
    # los tokens razonando, chocaba con la pared y había que repetir la
    # llamada entera. Un turno de 53 s. El tope convertía UNA llamada lenta en
    # DOS. Se quitó el mismo día.

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
    # `/api/brains/*`. Su personalidad y su conocimiento vienen de SU CRM.
    #
    # Con `crm_organization` puesta atiende a ESE negocio y a nadie más: un
    # despacho de otra organización viene firmado con otro secreto y se
    # rechaza con 401.
    #
    # Con `crm_organization` vacía atiende a todos los que el CRM le suscriba
    # (ver `multi_org` más abajo). Entonces `llm_api_key` no se usa: la llave
    # es de cada miembro y la pone el CRM, no viaja hasta aquí.
    vocero_mode: str = ""

    # Secreto del cerebro, el que genera el CRM en Ajustes → Cerebro. Firma lo
    # que llega y autentica lo que sale: el mismo secreto en las dos
    # direcciones, como en el contrato.
    crm_brain_secret: str = ""

    # Slug de la organización. Solo se usa para acompañar al secreto; el CRM
    # verifica que coincidan, así que no sirve de nada acertar uno sin el otro.
    #
    # **VACÍA en modo cloud = multi-organización.** Entonces esta Nea no
    # sirve a un negocio sino a todos los que el CRM le suscriba: cada
    # despacho dice de quién es, y la credencial para contestar se DERIVA del
    # secreto del despliegue (app/multiorg.py). Es lo que evita levantar un
    # contenedor por negocio.
    crm_organization: str = ""

    # Infra
    database_url: str = ""
    port: int = 8000

    # Desarrollo: loguear el JSON crudo de mensajes no-texto entrantes para
    # capturar los formatos reales de Meta (spec 002). Apagar al terminar.
    capture_payloads: bool = False

    @property
    def audio_mal_configurado(self) -> bool:
        """¿Proveedor propio con el modelo de audio de OpenAI?

        `whisper-1` no existe fuera de OpenAI, así que esta combinación hace
        que TODA nota de voz falle. Se detecta al arrancar y se avisa una vez,
        en vez de dejar que se descubra con un cliente esperando respuesta.
        """
        return bool(self.llm_base_url) and self.llm_transcribe_model == "whisper-1"

    @property
    def multi_org(self) -> bool:
        """¿Esta Nea atiende a varias organizaciones?

        Cloud + sin slug fijo. Se pide la ausencia de `crm_organization` y no
        una bandera nueva porque las dos cosas se contradicen: un slug fijo
        significa «solo este negocio», y una bandera aparte permitiría
        pedir las dos a la vez y dejar sin definir cuál gana.
        """
        return self.cloud_mode and not self.crm_organization.strip()

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

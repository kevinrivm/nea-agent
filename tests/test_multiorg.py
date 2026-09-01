"""Modo multi-organización: una Nea atendiendo a varios negocios.

Lo que se prueba aquí, en orden de importancia:

1. **La credencial derivada coincide con la del CRM.** Es un contrato entre dos
   repositorios; si se desvía un byte, Nea recibe 401 en todo y el síntoma es
   "no contesta", que no apunta a nada.
2. **Dos negocios no comparten conversación.** Aunque les escriba el mismo
   teléfono. Era el fallo que traía la unicidad global de `wa_identity`.
3. **Sin llave del miembro no hay turno.** Caer a la del entorno le cobraría a
   la cuenta del dueño de la plataforma el consumo de todos.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from dataclasses import fields

import pytest

from app import main
from app.config import Settings
from app.db import _conv_from_row
from app.multiorg import (
    CrmSinOrganizacion,
    LlmSinOrganizacion,
    RegistroDeOrganizaciones,
    credencial_derivada,
    ctx_de_organizacion,
    organizacion_del_despacho,
)
from app.state import AppContext, Conversation, MemoryStore, utcnow

SECRETO = "un-secreto-de-despliegue-largo-y-aburrido"
ORG = "org_abc123"


# ------------------------------------------------------ la credencial ------


def test_credencial_es_hmac_base64url_sin_relleno():
    """Réplica independiente del cálculo, para que no se pruebe consigo mismo."""
    esperado = base64.urlsafe_b64encode(
        hmac.new(
            SECRETO.encode(),
            f"vocero:cerebro:v1:{ORG}".encode(),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode()
    assert credencial_derivada(SECRETO, ORG) == esperado


def test_credencial_coincide_con_la_del_crm():
    """El valor que produce Node para estas mismas entradas.

    Fijado a mano y no calculado: si alguien cambia el dominio de separación o
    la codificación, esta prueba se pone roja aquí en vez de en producción con
    un 401 mudo. El valor sale de:

        createHmac("sha256", SECRETO)
          .update("vocero:cerebro:v1:org_abc123")
          .digest("base64url")
    """
    assert credencial_derivada(SECRETO, ORG) == (
        "_BqRKryMUdexcuLj_3RG1ARbgqr_LhXRSLJYmrw5VJ0"
    )


def test_credencial_distinta_por_organizacion():
    a = credencial_derivada(SECRETO, "org_a")
    b = credencial_derivada(SECRETO, "org_b")
    assert a != b


def test_credencial_distinta_al_rotar_el_secreto():
    """Rotar el secreto tiene que revocar: si no cambiara, no revocaría nada."""
    assert credencial_derivada(SECRETO, ORG) != credencial_derivada("otro", ORG)


def test_sin_relleno():
    """43 caracteres, sin '='. Node no lo pone y el CRM compara literal."""
    cred = credencial_derivada(SECRETO, ORG)
    assert "=" not in cred
    assert len(cred) == 43


# ------------------------------------------------- la organización ---------


def test_organizacion_del_despacho():
    payload = {"organization": {"id": "org_1", "slug": "mi-negocio"}}
    assert organizacion_del_despacho(payload) == ("org_1", "mi-negocio")


def test_organizacion_sin_slug_usa_el_id():
    """El slug puede faltar; el id no. Es lo que acepta el CRM en la cabecera."""
    assert organizacion_del_despacho({"organization": {"id": "org_1"}}) == (
        "org_1",
        "org_1",
    )


@pytest.mark.parametrize(
    "payload",
    [{}, {"organization": {}}, {"organization": {"slug": "sin-id"}}],
)
def test_sin_id_no_hay_organizacion(payload):
    """Sin id no hay credencial que derivar, así que no hay a quién contestar."""
    assert organizacion_del_despacho(payload) is None


# ------------------------------------------------------- el registro -------


def test_el_registro_reutiliza_el_cliente_por_organizacion():
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    uno = reg.cliente("org_1", "uno")
    otra_vez = reg.cliente("org_1", "uno")
    distinto = reg.cliente("org_2", "dos")
    assert uno is otra_vez
    assert uno is not distinto


def test_el_llm_apunta_al_crm_y_no_al_proveedor():
    """La llave del miembro NO viaja: piensa el CRM y Nea le pide.

    Es la propiedad de la que cuelga todo el diseño. Si esto se rompiera y el
    cliente volviera a apuntar a OpenRouter, haría falta una llave de verdad —
    y la única a mano sería la del entorno, o sea la del dueño de la
    plataforma pagando por todos.
    """
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    ajustes = Settings(llm_api_key="sk-del-dueño", llm_model="modelo/instancia")
    cliente = reg.llm(
        ORG, "mi-negocio", {"path": "/api/brains/llm", "model": "a/b"}, ajustes
    )

    assert str(cliente._client.base_url).startswith("http://crm/api/brains/llm")
    # La credencial derivada, no una llave de OpenRouter.
    assert cliente._client.api_key == credencial_derivada(SECRETO, ORG)
    assert "sk-del-dueño" not in str(cliente._client.api_key)


def test_el_llm_dice_de_que_organizacion_es_cada_llamada():
    """El CRM lo necesita para autenticar: sin la cabecera son todas 401."""
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    cliente = reg.llm(ORG, "mi-negocio", {"model": "a/b"}, Settings())
    cabeceras = cliente._client.default_headers or {}
    assert cabeceras.get("X-Vocero-Organization") == "mi-negocio"


def test_el_modelo_que_conversa_cae_al_de_la_instancia():
    """Un miembro que no eligió modelo tiene que poder conversar igual."""
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    ajustes = Settings(llm_model="modelo/instancia")
    cliente = reg.llm(ORG, "mi-negocio", {}, ajustes)
    assert cliente._model == "modelo/instancia"


def test_el_modelo_que_escucha_NO_cae_a_ninguno():
    """Sin modelo que oiga, no se transcribe. Y se dice.

    Caer al que conversa mandaría el audio a un modelo que no oye, y lo que
    volviera parecería una transcripción sin serlo. Quien la lea no tiene cómo
    saber que es inventada — es peor que decir que no se pudo.
    """
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    ajustes = Settings(llm_transcribe_model="oye/instancia")
    cliente = reg.llm(ORG, "mi-negocio", {"model": "a/b"}, ajustes)
    assert cliente._transcribe_model == ""


@pytest.mark.asyncio
async def test_sin_modelo_que_oiga_transcribir_falla_sin_llamar_a_nadie():
    """El guardarraíl del caso anterior, en el sitio donde muerde."""
    from app.llm import LlmExhausted, OpenAiLlm

    cliente = OpenAiLlm("cred", "a/b", transcribe_model="", base_url="http://crm")
    with pytest.raises(LlmExhausted, match="sin modelo de transcripción"):
        await cliente.transcribe(b"...", "audio/ogg")


def test_el_registro_cachea_el_llm_pero_no_entre_organizaciones():
    """Reutilizar el cliente ahorra sockets; compartirlo entre negocios
    los mezclaría, porque cada uno lleva su credencial y su cabecera.
    """
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    config = {"model": "a/b"}
    uno = reg.llm("org_1", "uno", dict(config), Settings())
    otra_vez = reg.llm("org_1", "uno", dict(config), Settings())
    de_otro = reg.llm("org_2", "dos", dict(config), Settings())
    assert uno is otra_vez
    assert uno is not de_otro


def test_cambiar_de_modelo_surte_efecto_en_el_turno_siguiente():
    """Sin invalidar nada a mano: la tupla de caché cambia con el modelo."""
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    antes = reg.llm(ORG, "n", {"model": "a/b"}, Settings())
    despues = reg.llm(ORG, "n", {"model": "c/d"}, Settings())
    assert antes is not despues

# --------------------------------------------------- el centinela ----------


@pytest.mark.asyncio
async def test_el_centinela_revienta_al_escribir():
    """Un camino que se olvide de armar el cliente tiene que fallar aquí y no
    escribirle en silencio al negocio equivocado."""
    crm = CrmSinOrganizacion()
    with pytest.raises(RuntimeError, match="sin saber de qué organización"):
        crm.send_message


@pytest.mark.asyncio
async def test_el_centinela_deja_arrancar_y_apagar():
    """Las dos que usa el ciclo de vida, y ninguna escribe nada."""
    crm = CrmSinOrganizacion()
    assert await crm.agenda_available() is True
    assert await crm.aclose() is None


# ------------------------------------------- el contexto de los workers ----


def _ctx(multi: bool) -> AppContext:
    settings = Settings(
        vocero_mode="cloud" if multi else "",
        crm_organization="" if multi else "unica",
        crm_brain_secret=SECRETO,
    )
    return AppContext(
        settings=settings,
        store=MemoryStore(),
        crm=CrmSinOrganizacion() if multi else object(),
        llm=object(),
        registro=RegistroDeOrganizaciones("http://crm", SECRETO) if multi else None,
    )


def test_fuera_de_multiorg_el_contexto_no_cambia():
    ctx = _ctx(multi=False)
    assert ctx_de_organizacion(ctx, "", "") is ctx


def test_en_multiorg_el_contexto_trae_el_cliente_de_esa_organizacion():
    ctx = _ctx(multi=True)
    resuelto = ctx_de_organizacion(ctx, "org_1", "uno")
    assert resuelto is not None
    assert resuelto.organizacion == ("org_1", "uno")
    assert resuelto.crm is ctx.registro.cliente("org_1", "uno")


def test_sin_organizacion_conocida_el_worker_se_calla():
    """Una fila vieja, escrita antes de esta versión. Contestar con la
    credencial equivocada sería escribirle a otro negocio."""
    ctx = _ctx(multi=True)
    assert ctx_de_organizacion(ctx, "", "") is None


# --------------------------------------- aislamiento de conversaciones -----


@pytest.mark.asyncio
async def test_el_mismo_telefono_en_dos_negocios_no_comparte_conversacion():
    """EL fallo que traía la unicidad global de wa_identity.

    La misma persona puede escribirle a dos negocios distintos. Con una sola
    conversación, el historial de uno entraba en el prompt del otro.
    """
    store = MemoryStore()
    telefono = "5215512345678"

    de_a = await store.get_or_create_conversation(telefono, "org_a", "a")
    de_b = await store.get_or_create_conversation(telefono, "org_b", "b")

    assert de_a.id != de_b.id
    assert de_a.organization_id == "org_a"
    assert de_b.organization_id == "org_b"

    # Y el historial no se cruza.
    await store.add_message(de_a.id, "user", "hola, soy cliente de A")
    de_b_otra_vez = await store.get_or_create_conversation(telefono, "org_b", "b")
    assert await store.recent_messages(de_b_otra_vez.id, 10) == []


@pytest.mark.asyncio
async def test_sin_organizacion_se_comporta_como_siempre():
    """Una Nea de un solo negocio: la misma identidad, la misma conversación."""
    store = MemoryStore()
    una = await store.get_or_create_conversation("5215512345678")
    otra = await store.get_or_create_conversation("5215512345678")
    assert una.id == otra.id


# ── El arranque ───────────────────────────────────────────────────────────
#
# Todo lo demás en esta suite construye el `AppContext` a mano. El arranque de
# verdad —el que arma sus propios recursos— no lo cubría nadie, y ahí es donde
# esto se rompió en producción: el proceso moría en el `lifespan` porque
# intentaba construir un cliente del modelo sin llave. Y en este modo NO hay
# llave a propósito.


class _StoreDePrueba:
    """Lo justo que el arranque le pide a la base."""

    async def connect(self) -> None:
        return None

    async def migrate(self, _directorio) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _WorkerQuieto:
    """Un worker que no hace nada: aquí se prueba el arranque, no el trabajo."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        return None


@pytest.fixture
def arranque_multiorg(monkeypatch):
    """Producción tal cual: cloud, sin organización fija y sin llave de nadie."""
    monkeypatch.setenv("VOCERO_MODE", "cloud")
    monkeypatch.setenv("CRM_BASE_URL", "http://vocero-cloud:3000")
    monkeypatch.setenv("CRM_BRAIN_SECRET", "secreto-del-despliegue-de-32-caracteres")
    monkeypatch.setenv("DATABASE_URL", "postgres://no-se-usa")
    # Vacías, no borradas: una variable borrada en Coolify llega aquí como el
    # default (""), y un `.env` en el repo no debe poder salvar esta prueba
    # dándole una llave que producción no tiene.
    for ausente in ("CRM_ORGANIZATION", "LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(ausente, "")
    monkeypatch.setattr(main, "PgStore", lambda *_a, **_k: _StoreDePrueba())
    monkeypatch.setattr(main, "RelayWorker", _WorkerQuieto)
    monkeypatch.setattr(main, "FollowupWorker", _WorkerQuieto)
    monkeypatch.setattr(main, "SenderWorker", _WorkerQuieto)


async def test_el_arranque_multiorg_no_muere_sin_llave(arranque_multiorg):
    """El fallo exacto que tiró el despliegue.

    Sin este arreglo, el SDK de OpenAI se niega a construirse sin credencial
    ("Missing credentials") y el proceso sale en el `lifespan`: contenedor
    unhealthy, rollback, y Nea nunca arriba.
    """
    app = main.create_app()
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx
        assert ctx.settings.multi_org
        assert isinstance(ctx.llm, LlmSinOrganizacion)


async def test_el_modelo_del_arranque_revienta_al_usarse(arranque_multiorg):
    """No degrada: revienta.

    `LlmExhausted` haría que el turno siguiera su camino de "no pude pensar" y
    un olvido de cableado se leería como un fallo del proveedor. Además, un
    cliente de verdad aquí le cobraría al dueño de la plataforma el consumo de
    sus miembros.
    """
    app = main.create_app()
    async with app.router.lifespan_context(app):
        with pytest.raises(RuntimeError, match="sin saber de qué organización"):
            await app.state.ctx.llm.complete([{"role": "user", "content": "hola"}])


def test_el_mapeo_de_conversacion_no_se_deja_ninguna_columna():
    """La hermana de la de `pending_send`, y esta duele más si falla.

    Si el mapeo dejara de leer `organization_id`, toda conversación releída de
    Postgres volvería «sin organización». El turno la trataría como la de una
    instalación de un solo negocio y le hablaría al CRM con la credencial
    equivocada: el historial de un negocio en el prompt de otro, que es
    exactamente lo que 004 existe para impedir.

    Contra `fields()` a propósito: el próximo campo que se añada no se puede
    olvidar en el mapeo sin que esto se ponga rojo.
    """
    ahora = utcnow()
    fila = {
        "id": 1,
        "wa_identity": "5215512345678",
        "crm_conversation_id": "cv_abc",
        "organization_id": "org_a",
        "organization_slug": "negocio-a",
        "phase": "descubrimiento",
        "greeted": True,
        "media_notice_sent": False,
        "followup_due_at": None,
        "followup_sent": False,
        "last_inbound_at": ahora,
        "stalled_at": None,
    }
    conv = _conv_from_row(fila)

    for campo in fields(Conversation):
        assert getattr(conv, campo.name) == fila[campo.name], (
            f"el mapeo no lee {campo.name}"
        )

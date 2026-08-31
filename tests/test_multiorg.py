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

import base64
import hashlib
import hmac

import pytest

from app.config import Settings
from app.multiorg import (
    CrmSinOrganizacion,
    RegistroDeOrganizaciones,
    credencial_derivada,
    ctx_de_organizacion,
    organizacion_del_despacho,
)
from app.state import AppContext, MemoryStore

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


def test_el_registro_cachea_el_llm_por_credencial():
    """Cachear por credencial y no por organización: rotar la llave debe surtir
    efecto en el turno siguiente, sin invalidar nada a mano."""
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    ajustes = Settings(llm_model="modelo/base", llm_transcribe_model="oye/algo")
    base = {"apiKey": "sk-1", "model": "a/b", "baseUrl": "http://x/v1"}

    primero = reg.llm(base, ajustes)
    assert reg.llm(dict(base), ajustes) is primero

    rotada = {**base, "apiKey": "sk-2"}
    assert reg.llm(rotada, ajustes) is not primero


def test_el_llm_rellena_huecos_con_los_ajustes_menos_la_llave():
    """Sin modelo elegido se usa el de la instancia. Sin llave NO se rellena:
    pensar con la del dueño de la plataforma es el gasto que esto evita."""
    reg = RegistroDeOrganizaciones("http://crm", SECRETO)
    ajustes = Settings(
        llm_api_key="sk-del-dueño",
        llm_model="modelo/instancia",
        llm_transcribe_model="oye/instancia",
    )
    cliente = reg.llm({"apiKey": "sk-del-miembro"}, ajustes)
    assert cliente._model == "modelo/instancia"
    assert cliente._transcribe_model == "oye/instancia"
    # La llave es la del miembro, jamás la del entorno.
    assert cliente._client.api_key == "sk-del-miembro"


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

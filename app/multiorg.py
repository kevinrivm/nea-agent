"""Modo multi-organización: una Nea atendiendo a varios negocios.

## El problema que resuelve

En modo cloud, Nea atendía a UNA organización: `CRM_BRAIN_SECRET`,
`CRM_ORGANIZATION` y `LLM_API_KEY` son valores únicos. Servir a diez negocios
significaba levantar diez Neas.

El atajo evidente —el mismo secreto para todas las organizaciones del CRM—
rompe el aislamiento del lado del CRM: allá cada miembro puede ver su secreto,
y con uno compartido cualquiera escribiría en la bandeja de otro.

## Cómo funciona

El CRM registra a esta Nea como un *despliegue* con UN secreto que ningún
miembro ve. Ese secreto:

1. **Firma los despachos.** Todos, sea cual sea la organización, así que se
   verifican con uno solo — que es lo que permite atenderlas a todas.
2. **Deriva la credencial de cada organización** para contestar:

       credencial(org) = HMAC-SHA256(secreto, "vocero:cerebro:v1:{org_id}")

Derivarla en vez de recibirla tiene una consecuencia práctica que importa: un
worker de seguimiento que despierta cuatro horas después la vuelve a calcular
igual. No hay token que caduque a media conversación ni credencial que guardar.

## Con qué piensa

**No con una llave propia, y tampoco con la del miembro.** El CRM piensa por
Nea: `/api/brains/llm` es un endpoint compatible con OpenAI que le pone la
credencial de esa organización y reenvía al proveedor.

Así la llave del miembro no sale nunca del CRM —una filtrada no se puede
desfiltrar— y, de paso, cuando el proveedor la rechaza es el CRM quien recibe
el 401 y puede pausarla en el panel de su dueño. Cuando la llave viajaba, ese
código se lo comía Nea y el miembro veía «activa» mientras nada funcionaba.

Del contexto de cada conversación sale la ruta y qué modelos puede usar. Ver
`crm_brains.py`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from app.crm_brains import BrainsCrmClient
from app.llm import OpenAiLlm
from app.profile import ProfileProvider

if TYPE_CHECKING:
    from app.state import AppContext

logger = logging.getLogger("nea.multiorg")

# Separación de dominio. Tiene que ser EXACTAMENTE la misma cadena que usa
# `src/server/brains/deployment.ts` en el CRM: es un contrato entre dos
# repositorios, y un guion de diferencia lo rompe entero sin decir por qué.
DOMINIO = "vocero:cerebro:v1"


def credencial_derivada(secreto_despliegue: str, organization_id: str) -> str:
    """La credencial de UNA organización dentro de este despliegue.

    base64url SIN relleno: es lo que produce `.digest("base64url")` de Node, y
    aquí se compara carácter a carácter contra lo que espera el CRM.
    """
    mac = hmac.new(
        secreto_despliegue.encode(),
        f"{DOMINIO}:{organization_id}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


class RegistroDeOrganizaciones:
    """Un cliente del CRM por organización, creado a demanda.

    Se cachean porque cada uno tiene su propio pool de conexiones HTTP: crear
    uno por mensaje abriría y cerraría sockets en cada turno contra el mismo
    CRM.

    La caché es por `organization_id` y no por slug: el slug es lo que viaja en
    la cabecera, pero el id es lo que firma la credencial. Si un miembro
    renombrara su subdominio, la credencial seguiría siendo la correcta.
    """

    def __init__(
        self,
        base_url: str,
        secreto_despliegue: str,
        *,
        nombre_por_defecto: str = "Nea",
        brief_path: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._secreto = secreto_despliegue
        self._nombre = nombre_por_defecto
        self._brief = brief_path
        self._clientes: dict[str, BrainsCrmClient] = {}
        self._perfiles: dict[str, ProfileProvider] = {}
        # Cacheados por la tupla completa y no por organizacion: si el miembro
        # cambia de modelo, la tupla cambia y el siguiente turno ya usa el
        # cliente nuevo sin que nadie tenga que invalidar nada.
        self._llms: dict[tuple[str, str, str, str], OpenAiLlm] = {}

    def cliente(self, organization_id: str, slug: str) -> BrainsCrmClient:
        cliente = self._clientes.get(organization_id)
        if cliente is None:
            cliente = BrainsCrmClient(
                self._base_url,
                credencial_derivada(self._secreto, organization_id),
                slug,
            )
            self._clientes[organization_id] = cliente
            logger.info(
                "organización %s (%s): cliente del CRM creado", slug, organization_id
            )
        return cliente

    def perfil(self, organization_id: str, slug: str) -> ProfileProvider:
        """El proveedor de perfil de esa organizacion, con su propia cache.

        Uno por organizacion y no uno por turno: `ProfileProvider` cachea con
        TTL, y rehacerlo en cada mensaje tiraria esa cache al suelo. Uno por
        organizacion mantiene la cache Y garantiza que el perfil de un
        negocio no se le sirva a otro.
        """
        proveedor = self._perfiles.get(organization_id)
        if proveedor is None:
            proveedor = ProfileProvider(
                self.cliente(organization_id, slug),
                default_name=self._nombre,
                brief_path=self._brief,
            )
            self._perfiles[organization_id] = proveedor
        return proveedor

    def llm(
        self,
        organization_id: str,
        slug: str,
        config: dict[str, Any],
        por_defecto: Any,
    ) -> OpenAiLlm:
        """El cliente del modelo, apuntando al CRM.

        No lleva la llave de nadie: lleva la credencial derivada de esta
        organización, la misma con la que Nea le habla al CRM para todo lo
        demás. El CRM le pone la llave del miembro al reenviar al proveedor.

        Para el SDK de OpenAI esto es un proveedor compatible más. Por eso el
        cambio cabe aquí y no se nota en el resto del turno.

        `por_defecto` son los ajustes de esta Nea y solo rellenan el modelo
        que conversa. El que TRANSCRIBE no se rellena: si el miembro no
        eligió uno que oiga, es mejor no transcribir que mandarle el audio a
        uno que no oye y quedarnos con lo que invente.
        """
        base_url = self._base_url.rstrip("/") + str(
            config.get("path") or "/api/brains/llm"
        )
        model = str(config.get("model") or por_defecto.llm_model)
        transcribe = str(config.get("transcribeModel") or "")
        credencial = credencial_derivada(self._secreto, organization_id)
        clave = (credencial, model, transcribe, base_url)
        cliente = self._llms.get(clave)
        if cliente is None:
            cliente = OpenAiLlm(
                credencial,
                model,
                transcribe_model=transcribe,
                base_url=base_url,
                default_headers={"X-Vocero-Organization": slug},
            )
            self._llms[clave] = cliente
        return cliente

    async def aclose(self) -> None:
        for cliente in self._clientes.values():
            await cliente.aclose()
        self._clientes.clear()
        # Los perfiles y los LLM cuelgan de esos clientes: dejarlos vivos
        # apuntando a conexiones cerradas es un fallo que solo aparece si la
        # app se reinicia en caliente, y entonces cuesta encontrarlo.
        self._perfiles.clear()
        self._llms.clear()


class CrmSinOrganizacion:
    """El "cliente" del CRM mientras no se sabe de qué organización.

    En modo multi-organización no existe un cliente del CRM por defecto: cada
    turno arma el suyo con la credencial de la organización que le toca. Pero
    `AppContext.crm` tiene que ser algo, y ese algo importa mucho.

    Si fuera un cliente de verdad —el de la primera organización, digamos—, un
    camino que se olvidara de cambiarlo escribiría en la bandeja EQUIVOCADA y
    nadie se enteraría: el mensaje sale, el CRM lo acepta, y aparece en el
    negocio que no era. Con esto, ese olvido revienta en el sitio exacto.

    Las dos excepciones son las que usa el arranque y el apagado, y ninguna
    escribe nada.
    """

    async def agenda_available(self) -> bool:
        """No se sonda: no hay con qué credencial preguntar.

        Se asume que sí, que es lo mismo que hace el sondeo cuando no puede
        concluir. Si resulta que no, el primer intento real recibe 404 y las
        herramientas de agenda contestan "aquí no se agenda" — un camino que ya
        existe y está probado.
        """
        return True

    async def aclose(self) -> None:
        return None

    def __getattr__(self, nombre: str) -> Any:
        raise RuntimeError(
            f"se intentó usar el CRM ({nombre}) sin saber de qué organización "
            f"es este turno: en modo multi-organización el cliente lo arma el "
            f"despacho, no el arranque"
        )

def ctx_de_organizacion(
    ctx: "AppContext",
    organization_id: str,
    organization_slug: str,
) -> "AppContext | None":
    """El contexto de un worker de fondo, apuntando a UNA organización.

    Los workers (seguimiento, envío diferido) despiertan mucho después del
    turno, cuando ya no hay despacho del que sacar de quién era el mensaje. Lo
    sacan de la fila que están procesando, que por eso guarda su organización.

    Fuera del modo multi-organización devuelve el contexto tal cual: una Nea de
    un solo negocio ya tiene el cliente correcto y no hay nada que resolver.

    None significa «no puedo saber de quién es esto». Pasa con filas escritas
    antes de esta versión, que no llevan organización. Callarse es lo correcto:
    contestar con la credencial equivocada sería escribirle al negocio que no
    era.
    """
    if not ctx.settings.multi_org:
        return ctx
    if not organization_id or ctx.registro is None:
        return None
    return replace(
        ctx,
        crm=ctx.registro.cliente(organization_id, organization_slug),
        profile=ctx.registro.perfil(organization_id, organization_slug),
        organizacion=(organization_id, organization_slug),
    )

def organizacion_del_despacho(payload: dict[str, Any]) -> tuple[str, str] | None:
    """`(id, slug)` de la organización a la que pertenece este despacho.

    El slug puede faltar —una organización recién creada podría no tenerlo— y
    entonces se usa el id también como slug, que es lo que acepta el CRM en la
    cabecera. El id, en cambio, es obligatorio: sin él no hay credencial que
    derivar, y contestar sería imposible.
    """
    org = payload.get("organization") or {}
    org_id = str(org.get("id") or "")
    if not org_id:
        return None
    return org_id, str(org.get("slug") or org_id)

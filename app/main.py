"""App FastAPI de Nea: lifespan (migraciones + workers) y /health.

`create_app()` sin argumentos es el camino de producción (uvicorn app.main:app):
el lifespan conecta Postgres, aplica migraciones y arranca los workers del
despacho, del seguimiento y de los envíos pendientes. Los tests inyectan un `AppContext` ya armado (MemoryStore, LLM
fake, CRM contra respx) y manejan los workers a mano.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agenda import SondaDeAgenda
from app.config import Settings
from app.crm import CrmClient
from app.crm_brains import BrainsCrmClient
from app.dispatch import router as dispatch_router
from app.dispatch_worker import run as run_dispatch_worker
from app.db import PgStore
from app.followup import FollowupWorker
from app.llm import LlmPorElCrm, OpenAiLlm
from app.multiorg import (
    CrmSinOrganizacion,
    LlmSinOrganizacion,
    RegistroDeOrganizaciones,
)
from app.profile import ProfileProvider
from app.sender import SenderWorker
from app.state import AppContext
from app.version import commit, version
from app.webhook_retirado import router as webhook_retirado_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nea.main")


class SinTokenPropio(logging.Filter):
    """Tacha el segmento secreto de `/webhook/<token>` en el log de uvicorn.

    Nea 1.x recibía el webhook de Meta en `/webhook/<VERIFY_TOKEN>`. La 2.0 ya
    no lo recibe (app/webhook_retirado.py), pero una instalación que actualiza
    sin mover el webhook sigue recibiendo esas peticiones, y uvicorn escribe
    la ruta y la query de cada una: el token viejo quedaría en los logs.
    """

    _RUTA = re.compile(r"(/webhook/)[^/\s\"?#]+")
    _QUERY = re.compile(r"(hub[._]verify_token=)[^&\s\"]+")

    def _limpia(self, texto: str) -> str:
        return self._QUERY.sub(r"\1***", self._RUTA.sub(r"\1***", texto))

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn desempaca `record.args` (cliente, método, ruta, versión,
        # código) para dar formato: se tacha DENTRO de la tupla. Reemplazar
        # el mensaje y dejar `args` en None le rompe el formateador.
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._limpia(a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.msg, str):
            record.msg = self._limpia(record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(SinTokenPropio())

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _identidad(settings: Settings | None) -> dict[str, Any]:
    """Qué Nea es: versión, commit y modo. Lo lee el CRM («Quién responde»).

    `commit` y `commitVerified` van igual que en el `/api/health` del CRM: solo
    si hay commit, y `commitVerified` es `true` únicamente si salió del build
    (app/version.py). Nada de secretos ni de URLs.
    """
    cuerpo: dict[str, Any] = {"version": version()}
    c = commit()
    if c.corto:
        cuerpo["commit"] = c.corto
        cuerpo["commitVerified"] = c.verificado
    if settings is not None:
        cuerpo["mode"] = (
            "multiorg"
            if settings.multi_org
            else "cloud" if settings.cloud_mode else "estándar"
        )
        # 036 — Con qué llave piensa: la del CRM (Ajustes → IA, por su proxy)
        # o una propia en LLM_API_KEY. Solo la palabra: ni la llave ni la URL.
        cuerpo["llm"] = (
            "crm" if settings.multi_org or settings.piensa_por_el_crm else "propia"
        )
    return cuerpo


def _llm_del_arranque(settings: Settings, perfil: ProfileProvider | None) -> Any:
    """El cliente del modelo del contexto del arranque.

    - Multi-organización: un centinela. No hay modelo por defecto —Nea no
      lleva la llave de nadie— y uno de verdad aquí le cobraría al dueño de la
      plataforma el consumo de sus miembros si algún camino se olvidara de
      cambiarlo. Cada turno arma el suyo (app/multiorg.py).
    - Contra la raíz sin `LLM_API_KEY` (036): piensa por el CRM, con
      `CRM_BOT_API_KEY` y el modelo que diga el perfil.
    - Con `LLM_API_KEY`: su proveedor, como siempre.
    """
    if settings.multi_org:
        return LlmSinOrganizacion()
    if settings.piensa_por_el_crm:
        return LlmPorElCrm(
            settings.crm_base_url,
            settings.crm_bot_api_key,
            perfil,
            model=settings.llm_model,
            transcribe_model=settings.modelo_de_transcripcion,
            reasoning_effort=settings.llm_reasoning_effort,
            provider_sort=settings.llm_provider_sort,
            timeout=settings.llm_timeout_seconds,
        )
    return OpenAiLlm(
        settings.llm_api_key,
        settings.llm_model,
        transcribe_model=settings.modelo_de_transcripcion,
        base_url=settings.llm_base_url or None,
        reasoning_effort=settings.llm_reasoning_effort,
        provider_sort=settings.llm_provider_sort,
        timeout=settings.llm_timeout_seconds,
    )


# Variables de Nea 1.x que la 2.0 ya no lee. Una variable que deja de leerse
# no avisa sola: el contenedor arranca igual y quien instala cree que hace algo.
OBSOLETAS = (
    "META_APP_SECRET",
    "VERIFY_TOKEN",
    "CRM_WEBHOOK_URL",
    "RELAY_BACKOFF_CAP_SECONDS",
    "COALESCE_SECONDS",
    "TYPING_DELAY_SECONDS",
    "TURN_RETRY_DELAYS",
)


def _avisar_obsoletas() -> None:
    puestas = [v for v in OBSOLETAS if os.environ.get(v, "").strip()]
    if puestas:
        logger.warning(
            "estas variables ya no se usan desde la 2.0 y se pueden quitar: %s. "
            "Nea ya no recibe el webhook de Meta: lo recibe el CRM y se lo "
            "despacha (BRAIN_DISPATCH_URL en el CRM → /vocero/dispatch de Nea).",
            ", ".join(puestas),
        )


def create_app(ctx: AppContext | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_resources = app.state.ctx is None
        if own_resources:
            settings = Settings()
            _avisar_obsoletas()
            store = PgStore(settings.database_url, settings.database_schema)
            await store.connect()
            await store.migrate(MIGRATIONS_DIR)
            logger.info("migraciones aplicadas — DB lista")
            # Tres modos. Los tres reciben el despacho; cambia con quién
            # contestan:
            #
            # - multi-organización: no hay cliente fijo. Cada turno arma el
            #   suyo con la credencial derivada de SU organización, y el
            #   centinela hace que un camino que se olvide de armarlo
            #   reviente en vez de escribirle al negocio equivocado.
            # - cloud de un negocio: el cliente de una sola organización.
            # - sin bandera: Vocero raíz, por `/api/bot/*`.
            registro = None
            if settings.multi_org:
                registro = RegistroDeOrganizaciones(
                    settings.crm_base_url,
                    settings.crm_brain_secret,
                    nombre_por_defecto=settings.agent_name,
                    brief_path=settings.brief_path or None,
                )
                crm = CrmSinOrganizacion()
            elif settings.cloud_mode:
                crm = BrainsCrmClient(
                    settings.crm_base_url,
                    settings.crm_brain_secret,
                    settings.crm_organization,
                )
            else:
                crm = CrmClient(settings.crm_base_url, settings.crm_bot_api_key)
            # En multi-organización el perfil es de cada negocio y lo sirve el
            # registro; un proveedor global aquí serviría el perfil de uno a
            # todos.
            perfil = (
                None
                if settings.multi_org
                else ProfileProvider(
                    crm,
                    default_name=settings.agent_name,
                    brief_path=settings.brief_path or None,
                )
            )
            app.state.ctx = AppContext(
                settings=settings,
                store=store,
                crm=crm,
                # Mismo criterio que el CRM de arriba (ver `_llm_del_arranque`).
                llm=_llm_del_arranque(settings, perfil),
                profile=perfil,
                registro=registro,
            )
        c: AppContext = app.state.ctx

        if c.settings.audio_mal_configurado:
            logger.info(
                "LLM_TRANSCRIBE_MODEL sigue en 'whisper-1', que solo existe en "
                "OpenAI, y aquí el audio viaja dentro del chat (LLM_BASE_URL o "
                "el CRM): las notas de voz se transcriben con %s. Para otro, "
                "pon en esa variable un modelo que acepte audio.",
                c.settings.modelo_de_transcripcion,
            )
        if c.settings.piensa_por_el_crm:
            logger.info(
                "LLM_API_KEY vacía: Nea piensa por el CRM (Ajustes → IA, "
                "/api/bot/llm) con el modelo que diga su perfil"
            )

        if own_resources:
            # ¿Este CRM agenda? Vocero trae el motor detrás de una bandera de
            # despliegue y viene apagado por defecto. Se pregunta aquí, en vez
            # de descubrirlo lead por lead, para que el primero que escriba ya
            # reciba el comportamiento correcto en vez de una promesa de cita
            # que no se puede cumplir. Y la respuesta caduca: los turnos la
            # vuelven a pedir cada AGENDA_PROBE_TTL_SECONDS (app/agenda.py),
            # así que encender AGENDA en el CRM ya no exige reiniciar Nea.
            c.agenda_sonda = SondaDeAgenda(
                c.crm, ttl=c.settings.agenda_probe_ttl_seconds
            )
            c.agenda_enabled = await c.agenda_sonda.vigente()
            logger.info(
                "agenda del CRM: %s (se vuelve a preguntar cada %.0f s)",
                "disponible" if c.agenda_enabled else "APAGADA — Nea no ofrecerá citas",
                c.agenda_sonda.ttl,
            )

        followup_worker = FollowupWorker(c)
        sender_worker = SenderWorker(c)
        workers = [
            asyncio.create_task(run_dispatch_worker(c), name="dispatch-worker"),
            asyncio.create_task(followup_worker.run(), name="followup-worker"),
            asyncio.create_task(sender_worker.run(), name="sender-worker"),
        ]
        logger.info(
            "Nea arriba (%s): despacho + seguimiento + envíos pendientes corriendo",
            (
                "cloud multi-organización"
                if c.settings.multi_org
                else "cloud" if c.settings.cloud_mode else "estándar"
            ),
        )
        try:
            yield
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if own_resources:
                await c.crm.aclose()
                if c.registro is not None:
                    await c.registro.aclose()
                await c.store.aclose()

    app = FastAPI(title="Nea — agente de agendamiento para WhatsApp", lifespan=lifespan)
    app.state.ctx = ctx
    # La única entrada, en los dos modos (spec 031). Y el webhook de Meta, que
    # ya no se recibe, contesta 503 para que Meta reintente mientras quien
    # instala lo devuelve al CRM.
    app.include_router(dispatch_router)
    app.include_router(webhook_retirado_router)

    @app.get("/health")
    async def health(request: Request):  # type: ignore[no-untyped-def]
        # El código HTTP lo decide SOLO la base: es lo que mira el HEALTHCHECK
        # del Dockerfile. Lo demás es para quien lo lea (el CRM).
        c: AppContext | None = request.app.state.ctx
        if c is None:
            return JSONResponse(
                {"status": "starting", **_identidad(None)}, status_code=503
            )
        identidad = _identidad(c.settings)
        try:
            await c.store.ping()
        except Exception:
            logger.exception("health: la DB no responde")
            return JSONResponse(
                {"status": "degraded", "db": "error", **identidad}, status_code=503
            )
        cuerpo: dict[str, Any] = {"status": "ok", "db": "ok", **identidad}
        # 031 — ¿Meta le sigue mandando el webhook a Nea? Si sí, alguien
        # actualizó sin devolvérselo al CRM (app/webhook_retirado.py).
        ultimo = c.webhook_meta_ultimo
        cuerpo["webhookDeMeta"] = {
            "ultimoEn": ultimo.isoformat(timespec="seconds") if ultimo else None
        }
        return cuerpo

    return app


app = create_app()

"""App FastAPI de Nea: lifespan (migraciones + workers) y /health.

`create_app()` sin argumentos es el camino de producción (uvicorn app.main:app):
el lifespan conecta Postgres, aplica migraciones y arranca los workers de relay
y seguimiento. Los tests inyectan un `AppContext` ya armado (MemoryStore, LLM
fake, CRM contra respx) y manejan los workers a mano.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.coalesce import Coalescer
from app.config import Settings
from app.crm import CrmClient
from app.crm_brains import BrainsCrmClient
from app.dispatch import router as dispatch_router
from app.db import PgStore
from app.followup import FollowupWorker
from app.llm import OpenAiLlm
from app.multiorg import (
    CrmSinOrganizacion,
    LlmSinOrganizacion,
    RegistroDeOrganizaciones,
)
from app.profile import ProfileProvider
from app.relay import RelayWorker
from app.sender import SenderWorker
from app.state import AppContext
from app.turn import handle_flush
from app.webhook import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nea.main")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _wire_coalescer(ctx: AppContext) -> None:
    if ctx.coalescer is None:
        ctx.coalescer = Coalescer(
            ctx.settings.coalesce_seconds, partial(handle_flush, ctx)
        )


def create_app(ctx: AppContext | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_resources = app.state.ctx is None
        if own_resources:
            settings = Settings()
            store = PgStore(settings.database_url)
            await store.connect()
            await store.migrate(MIGRATIONS_DIR)
            logger.info("migraciones aplicadas — DB lista")
            # Tres modos, y solo el primero es nuevo:
            #
            # - multi-organización: no hay cliente fijo. Cada turno arma el
            #   suyo con la credencial derivada de SU organización, y el
            #   centinela hace que un camino que se olvide de armarlo
            #   reviente en vez de escribirle al negocio equivocado.
            # - cloud de un negocio: el cliente de una sola organización.
            # - sin bandera: exactamente lo de toda la vida.
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
            app.state.ctx = AppContext(
                settings=settings,
                store=store,
                crm=crm,
                # Mismo criterio que el CRM de arriba: en multi-organización
                # no hay modelo por defecto. Además de que no habría con qué
                # construirlo —Nea no lleva la llave de nadie—, uno de verdad
                # aquí le cobraría al dueño de la plataforma el consumo de sus
                # miembros si algún camino se olvidara de cambiarlo.
                llm=(
                    LlmSinOrganizacion()
                    if settings.multi_org
                    else OpenAiLlm(
                        settings.llm_api_key,
                        settings.llm_model,
                        transcribe_model=settings.llm_transcribe_model,
                        base_url=settings.llm_base_url or None,
                    )
                ),
                # En multi-organización el perfil es de cada negocio y lo
                # sirve el registro; un proveedor global aquí serviría el
                # perfil de uno a todos.
                profile=(
                    None
                    if settings.multi_org
                    else ProfileProvider(
                        crm,
                        default_name=settings.agent_name,
                        brief_path=settings.brief_path or None,
                    )
                ),
                registro=registro,
            )
        c: AppContext = app.state.ctx
        _wire_coalescer(c)

        if c.settings.audio_mal_configurado:
            logger.warning(
                "OPENAI_BASE_URL apunta a otro proveedor pero "
                "OPENAI_TRANSCRIBE_MODEL sigue en 'whisper-1', que solo existe "
                "en OpenAI: las notas de voz van a fallar. Pon ahí un modelo "
                "que acepte audio (no todos oyen — los GLM, por ejemplo, no)."
            )

        if own_resources:
            # ¿Este CRM agenda? Vocero trae el motor detrás de una bandera de
            # despliegue y viene apagado por defecto. Se pregunta una vez, aquí,
            # en vez de descubrirlo lead por lead: así el primero que escriba ya
            # recibe el comportamiento correcto en vez de una promesa de cita
            # que no se puede cumplir.
            c.agenda_enabled = await c.crm.agenda_available()
            logger.info(
                "agenda del CRM: %s",
                "disponible" if c.agenda_enabled else "APAGADA — Nea no ofrecerá citas",
            )

        relay_worker = RelayWorker(c.store, c.settings.crm_webhook_url, c.relay_wake)
        followup_worker = FollowupWorker(c)
        sender_worker = SenderWorker(c)
        workers = [
            asyncio.create_task(followup_worker.run(), name="followup-worker"),
            asyncio.create_task(sender_worker.run(), name="sender-worker"),
        ]
        # El relay reenvía al CRM el payload crudo de Meta. En cloud el CRM YA
        # tiene el mensaje —él lo recibió y él nos lo despachó—, así que
        # reenviárselo sería duplicarlo en la bandeja del cliente.
        if not c.settings.cloud_mode:
            workers.insert(
                0, asyncio.create_task(relay_worker.run(), name="relay-worker")
            )
        logger.info(
            "Nea arriba (%s): %sfollowup + sender corriendo",
            (
                "cloud multi-organización"
                if c.settings.multi_org
                else "cloud" if c.settings.cloud_mode else "meta"
            ),
            "" if c.settings.cloud_mode else "relay + ",
        )
        try:
            yield
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await relay_worker.aclose()
            if c.coalescer is not None:
                await c.coalescer.aclose()
            if own_resources:
                await c.crm.aclose()
                if c.registro is not None:
                    await c.registro.aclose()
                await c.store.aclose()

    app = FastAPI(title="Nea — agente de agendamiento para WhatsApp", lifespan=lifespan)
    app.state.ctx = ctx
    if ctx is not None:
        _wire_coalescer(ctx)
    app.include_router(webhook_router)
    # La entrada del despacho solo existe en cloud. Montarla siempre dejaría
    # una ruta pública de más en cada instalación que no la usa.
    if (ctx.settings if ctx is not None else Settings()).cloud_mode:
        app.include_router(dispatch_router)

    @app.get("/health")
    async def health(request: Request):  # type: ignore[no-untyped-def]
        c: AppContext | None = request.app.state.ctx
        if c is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        try:
            await c.store.ping()
        except Exception:
            logger.exception("health: la DB no responde")
            return JSONResponse(
                {"status": "degraded", "db": "error"}, status_code=503
            )
        return {"status": "ok", "db": "ok"}

    return app


app = create_app()

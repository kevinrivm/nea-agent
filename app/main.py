"""App FastAPI de Nea: lifespan (migraciones + workers) y /health.

`create_app()` sin argumentos es el camino de producción (uvicorn app.main:app):
el lifespan conecta Postgres, aplica migraciones y arranca los workers de relay
y seguimiento. Los tests inyectan un `AppContext` ya armado (MemoryStore, LLM
fake, CRM contra respx) y manejan los workers a mano.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agenda import SondaDeAgenda
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
from app.turn import cancelar_pendientes, handle_flush, reanudar_pendientes
from app.version import commit, version
from app.webhook import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nea.main")


class SinTokenDelWebhook(logging.Filter):
    """Tacha el token del webhook del CRM en los logs de httpx.

    httpx escribe a INFO la URL de cada petición, y la del relay lleva el
    `META_WEBHOOK_VERIFY_TOKEN` del CRM en la ruta
    (`/api/webhooks/wa/<token>`): cada mensaje entrante lo dejaba en los logs
    del contenedor. El resto de la línea (método, ruta, código) se queda,
    que es lo que sirve para depurar.
    """

    _TOKEN = re.compile(r"(/api/webhooks/wa/)[^/\s\"?#]+")

    def filter(self, record: logging.LogRecord) -> bool:
        mensaje = record.getMessage()
        limpio = self._TOKEN.sub(r"\1***", mensaje)
        if limpio != mensaje:
            record.msg, record.args = limpio, None
        return True


logging.getLogger("httpx").addFilter(SinTokenDelWebhook())

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _wire_coalescer(ctx: AppContext) -> None:
    if ctx.coalescer is None:
        ctx.coalescer = Coalescer(
            ctx.settings.coalesce_seconds, partial(handle_flush, ctx)
        )


def _identidad(settings: Settings | None) -> dict[str, Any]:
    """Qué Nea es: versión, commit y modo. Lo lee el CRM («Quién responde»).

    `commit` y `commitVerified` van igual que en el `/api/health` del CRM: solo
    si hay commit, y `commitVerified` es `true` únicamente si salió del build
    (app/version.py). Nada de secretos ni de URLs: el webhook del CRM lleva
    su token en la ruta.
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
    return cuerpo


async def _estado_del_relay(ctx: AppContext) -> dict[str, Any] | None:
    """La cola del relay al CRM. `None` si no se pudo leer: la DB ya
    contestó al ping, y un fallo aquí no debe tumbar el healthcheck."""
    try:
        stats = await ctx.store.relay_stats()
    except Exception:
        logger.exception("health: no pude leer la cola del relay")
        return None
    return {
        "pendientes": stats.pendientes,
        "masViejoSegundos": stats.mas_viejo_segundos,
        "ultimoErrorEn": (
            stats.ultimo_error_en.isoformat(timespec="seconds")
            if stats.ultimo_error_en is not None
            else None
        ),
    }


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
                        reasoning_effort=settings.llm_reasoning_effort,
                        provider_sort=settings.llm_provider_sort,
                        timeout=settings.llm_timeout_seconds,
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

        relay_worker = RelayWorker(
            c.store,
            c.settings.crm_webhook_url,
            c.relay_wake,
            backoff_cap=c.settings.relay_backoff_cap_seconds,
            # El CRM volvió: los turnos que lo esperaban no aguardan su espera.
            al_volver=partial(reanudar_pendientes, c),
        )
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
            await cancelar_pendientes(c)
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
        # El código HTTP lo decide SOLO la base: es lo que mira el HEALTHCHECK
        # del Dockerfile, y una cola atrasada del relay no se arregla
        # reiniciando el contenedor. Lo demás es para quien lo lea (el CRM).
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
        # El relay solo corre en el modo de siempre (en cloud el CRM ya tiene
        # el mensaje): ahí, ¿cuántos entrantes esperan llegar al CRM, desde
        # cuándo, y cuándo falló la última entrega?
        if not c.settings.cloud_mode:
            cuerpo["relay"] = await _estado_del_relay(c)
        return cuerpo

    return app


app = create_app()

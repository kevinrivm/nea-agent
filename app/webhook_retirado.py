"""El webhook de Meta que Nea ya no recibe (2.0).

Desde la 2.0 Meta le manda el webhook al CRM, y el CRM le despacha a Nea la
ráfaga (app/dispatch.py). Pero quien actualiza Nea antes de devolverle el
webhook al CRM sigue recibiendo aquí los mensajes de sus clientes, y un 404
los perdería sin que nadie se enterara.

Por eso esta ruta contesta **503**: Meta reintenta en vez de dar el mensaje por
entregado, y en cuanto el webhook apunta al CRM los mensajes siguen llegando.
Mientras tanto, un error en el log —como mucho uno por minuto— dice qué hacer,
y `/health` enseña cuándo llegó el último (`webhookDeMeta.ultimoEn`).

La verificación (`GET`) responde 404: suscribir a Nea a Meta ya no tiene
sentido, y un challenge contestado lo haría parecer correcto.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.state import AppContext, utcnow

logger = logging.getLogger("nea.webhook_retirado")

router = APIRouter()

AVISO_CADA = timedelta(minutes=1)

AVISO = (
    "Meta le está mandando el webhook a Nea, y desde la 2.0 Nea solo recibe el "
    "despacho del CRM. Contesté 503 para que Meta lo reintente: devuélvele el "
    "webhook al CRM (override de la WABA o del número hacia "
    "/api/webhooks/wa/<token> del CRM) y pon BRAIN_DISPATCH_URL en el CRM "
    "apuntando a /vocero/dispatch de Nea."
)


def _registrar(ctx: AppContext) -> JSONResponse:
    ahora = utcnow()
    anterior = ctx.webhook_meta_ultimo
    ctx.webhook_meta_ultimo = ahora
    if anterior is None or ahora - anterior >= AVISO_CADA:
        logger.error(AVISO)
    return JSONResponse(
        {"error": "webhook_retirado", "detalle": "Nea 2.0 recibe el despacho del CRM"},
        status_code=503,
    )


@router.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    return _registrar(request.app.state.ctx)


@router.post("/webhook/{segmento}")
async def webhook_con_segmento(segmento: str, request: Request) -> JSONResponse:
    return _registrar(request.app.state.ctx)


@router.get("/webhook")
async def verificacion() -> JSONResponse:
    return JSONResponse({"error": "no_encontrado"}, status_code=404)


@router.get("/webhook/{segmento}")
async def verificacion_con_segmento(segmento: str) -> JSONResponse:
    return JSONResponse({"error": "no_encontrado"}, status_code=404)

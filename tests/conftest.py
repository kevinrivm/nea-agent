"""Fixtures compartidas: MemoryStore, LLM fake, CRM real contra respx, app ASGI.

Desde la 2.0 (spec 031) a Nea solo le entran despachos del CRM. `despachar`
arma el evento con la forma de la raíz (`armarEvento` de vocero-crm), lo firma
con `CRM_BOT_API_KEY` como el CRM, lo entrega por `/vocero/dispatch` y vacía
la cola durable como lo haría su worker.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import hmac
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crm import CrmClient
from app.llm import LlmReply
from app.main import create_app
from app.state import AppContext, MemoryStore

CRM_URL = "http://crm.test"

IDENTITY = "525550001111"
CRM_CONV_ID = "cv_test1"


class FakeLLM:
    """LLM determinista: entrega respuestas en cola y registra las llamadas."""

    def __init__(self, replies: list[LlmReply] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self.raise_exc: Exception | None = None
        self.transcriptions: list[dict[str, Any]] = []
        self.transcript_text = "hola, tengo una clínica y se me llena el WhatsApp"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply:
        self.calls.append({"messages": messages, "tools": tools})
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.replies:
            return self.replies.pop(0)
        return LlmReply(content="¡Hola! ¿A qué se dedica tu negocio?")

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str:
        self.transcriptions.append(
            {"bytes": len(data), "mime": mime, "filename": filename}
        )
        return self.transcript_text


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        crm_base_url=CRM_URL,
        crm_bot_api_key="test-key",
        llm_api_key="sk-test",
        llm_model="gpt-test",
        history_window=10,
        allowed_wa_ids="",
        followup_hours=4.0,
        database_url="",
        port=8000,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_ctx(settings: Settings | None = None, llm: FakeLLM | None = None) -> AppContext:
    settings = settings or make_settings()
    return AppContext(
        settings=settings,
        store=MemoryStore(),
        crm=CrmClient(settings.crm_base_url, settings.crm_bot_api_key),
        llm=llm or FakeLLM(),
    )


@pytest.fixture
def ctx() -> AppContext:
    return make_ctx()


@pytest.fixture
async def client(ctx: AppContext):
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        yield c
    await ctx.crm.aclose()


_ids_de_despacho = iter(range(1, 10**9))


def evento_despacho(
    text: str | None = "hola",
    wamid: str = "wamid.001",
    frm: str = IDENTITY,
    msg_type: str = "text",
    conv_id: str = CRM_CONV_ID,
    channel: str = "whatsapp",
    dispatch_id: str | None = None,
    mensajes: list[dict[str, Any]] | None = None,
    prueba: bool = False,
) -> dict[str, Any]:
    """Un despacho de la raíz, con la forma y el orden de claves del CRM.

    Los tipos de adjunto llevan lo que manda la raíz desde la 031: `mediaId`,
    `mimeType` y `caption` con binario; `fileName` si lo hay; `location` y
    `contacts` en su tipo.
    """
    m: dict[str, Any] = {
        "id": wamid,
        "at": "2026-10-19T05:10:03.000Z",
        "type": msg_type,
        "text": text if msg_type == "text" else None,
        "mediaId": None,
    }
    if msg_type == "image":
        m.update(mediaId="media123", mimeType="image/jpeg", caption=None)
    elif msg_type == "audio":
        m.update(mediaId="media-audio-1", mimeType="audio/ogg; codecs=opus", caption=None)
    elif msg_type == "document":
        m.update(mediaId="media-doc-1", mimeType="application/pdf", caption=None, fileName="menu.pdf")
    elif msg_type == "sticker":
        m.update(mediaId="media-stk-1", mimeType="image/webp", caption=None)
    elif msg_type == "video":
        m.update(mediaId="media-vid-1", mimeType="video/mp4", caption=None)
    elif msg_type == "location":
        m["location"] = {"latitude": 20.69, "longitude": -101.36}
    elif msg_type == "contacts":
        m["contacts"] = [{"name": "Luis Gómez"}]
    return {
        "dispatchId": dispatch_id or f"dsp_test{next(_ids_de_despacho)}",
        "organization": {"id": "org_test", "slug": "negocio-de-prueba"},
        "conversation": {
            "id": conv_id,
            "channel": channel,
            "aiEnabled": True,
            "windowOpen": True,
            "windowExpiresAt": "2026-10-20T05:10:03.000Z",
            **({"test": True} if prueba else {}),
        },
        "contact": {"id": "ct_1", "displayName": "Lead de Prueba", "identity": frm},
        "messages": mensajes if mensajes is not None else [m],
        "firstMessageAt": "2026-10-19T05:10:03.000Z",
        "lastMessageAt": "2026-10-19T05:10:03.000Z",
    }


def firmar(cuerpo: bytes, secreto: str = "test-key") -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


async def vaciar_cola(ctx: AppContext) -> None:
    """Lo que hace el worker del despacho: atender la cola hasta vaciarla."""
    from app.dispatch_worker import drain_one

    while await drain_one(ctx):
        pass


async def despachar(
    client: httpx.AsyncClient,
    ctx: AppContext | None = None,
    *,
    esperar: bool = False,
    **kwargs: Any,
) -> httpx.Response:
    """Entrega un despacho firmado y deja corriendo su turno, como el worker.

    Sin `esperar` el turno corre en segundo plano (las pruebas miden con
    `asyncio.sleep`, como cuando el turno salía del coalescer). Sin `ctx`, el
    de la app detrás de `client`.
    """
    ctx = ctx or client._transport.app.state.ctx  # type: ignore[attr-defined]
    cuerpo = json.dumps(evento_despacho(**kwargs)).encode()
    resp = await client.post(
        "/vocero/dispatch",
        content=cuerpo,
        headers={"x-vocero-signature": firmar(cuerpo, ctx.settings.dispatch_secret)},
    )
    tarea = asyncio.create_task(vaciar_cola(ctx))
    if esperar:
        await tarea
    return resp


def crm_context(
    ai_enabled: bool = True,
    window_open: bool = True,
    conv_id: str = CRM_CONV_ID,
    ad_headline: str | None = None,
) -> dict[str, Any]:
    return {
        "contact": {
            "id": "ct_1",
            "name": "Lead de Prueba",
            "waIdentity": IDENTITY,
            "phone": IDENTITY,
            "ficha": {},
        },
        "conversation": {
            "id": conv_id,
            "aiEnabled": ai_enabled,
            "windowOpen": window_open,
            "windowExpiresAt": "2026-07-18T00:00:00Z",
            "handoffAt": None,
        },
        "lead": {"id": "ld_1", "stageName": "Nuevo"},
        "adOrigen": {"headline": ad_headline, "hasCtwaClid": True}
        if ad_headline
        else None,
        "booking": {"next": None},
    }


def mock_crm_basics(respx_mock: Any, **context_kwargs: Any) -> dict[str, Any]:
    """Registra las rutas típicas del CRM y regresa los handles."""
    routes = {
        "context": respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
            return_value=httpx.Response(200, json=crm_context(**context_kwargs))
        ),
        "messages": respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
            return_value=httpx.Response(200, json={"messageId": "msg_1"})
        ),
        "ficha": respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
            return_value=httpx.Response(200, json={"ficha": {}, "stageMoved": False})
        ),
        "handoff": respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
            # Fiel al CRM real (002): reason fuera del catálogo cerrado = 422.
            side_effect=lambda request: httpx.Response(
                200
                if json.loads(request.content).get("reason")
                in {"cliente", "modelo", "error", "ventana", "hostilidad"}
                else 422,
                json={},
            )
        ),
        "typing": respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
            return_value=httpx.Response(200, json={"ok": True})
        ),
        "media": respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/media/").mock(
            return_value=httpx.Response(
                200, content=b"fake-bytes", headers={"content-type": "image/jpeg"}
            )
        ),
    }
    return routes

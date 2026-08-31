"""Fixtures compartidas: MemoryStore, LLM fake, CRM real contra respx, app ASGI."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crm import CrmClient
from app.llm import LlmReply
from app.main import create_app
from app.state import AppContext, MemoryStore

CRM_URL = "http://crm.test"
CRM_WEBHOOK_URL = "http://crm.test/api/webhooks/wa/tok-crm"

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
        verify_token="vtoken",
        meta_app_secret="",
        crm_base_url=CRM_URL,
        crm_webhook_url=CRM_WEBHOOK_URL,
        crm_bot_api_key="test-key",
        llm_api_key="sk-test",
        llm_model="gpt-test",
        history_window=10,
        allowed_wa_ids="",
        coalesce_seconds=0.05,
        typing_delay_seconds=0.01,
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


def wa_payload(
    text: str = "hola",
    wamid: str = "wamid.001",
    frm: str = IDENTITY,
    msg_type: str = "text",
    referral_headline: str | None = None,
) -> dict[str, Any]:
    """Payload realista del webhook de WhatsApp Cloud API."""
    msg: dict[str, Any] = {
        "from": frm,
        "id": wamid,
        "timestamp": "1752700000",
        "type": msg_type,
    }
    if msg_type == "text":
        msg["text"] = {"body": text}
    elif msg_type == "image":
        msg["image"] = {"id": "media123", "mime_type": "image/jpeg"}
    elif msg_type == "audio":
        msg["audio"] = {
            "id": "media-audio-1",
            "mime_type": "audio/ogg; codecs=opus",
            "voice": True,
        }
    elif msg_type == "document":
        msg["document"] = {
            "id": "media-doc-1",
            "mime_type": "application/pdf",
            "filename": "menu.pdf",
        }
    elif msg_type == "sticker":
        msg["sticker"] = {
            "id": "media-stk-1",
            "mime_type": "image/webp",
            "animated": True,
        }
    elif msg_type == "location":
        msg["location"] = {"latitude": 20.69, "longitude": -101.36}
    elif msg_type == "video":
        msg["video"] = {"id": "media-vid-1", "mime_type": "video/mp4"}
    elif msg_type == "reaction":
        msg["reaction"] = {"message_id": "wamid.prev", "emoji": "❤️"}
    if referral_headline:
        msg["referral"] = {
            "source_type": "ad",
            "headline": referral_headline,
            "ctwa_clid": "clid123",
        }
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "123"},
                            "contacts": [
                                {"wa_id": frm, "profile": {"name": "Lead de Prueba"}}
                            ],
                            "messages": [msg],
                        },
                    }
                ],
            }
        ],
    }


def wa_body(**kwargs: Any) -> bytes:
    return json.dumps(wa_payload(**kwargs)).encode("utf-8")


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
        "relay": respx_mock.post(CRM_WEBHOOK_URL).mock(
            return_value=httpx.Response(200)
        ),
        "media": respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/media/").mock(
            return_value=httpx.Response(
                200, content=b"fake-bytes", headers={"content-type": "image/jpeg"}
            )
        ),
    }
    return routes

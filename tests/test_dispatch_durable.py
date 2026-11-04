import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.main import create_app
from app.dispatch_worker import drain_one
from app.media import _pdf_text
from tests.conftest import make_ctx, make_settings


def payload():
    return {"dispatchId": "dsp_durable", "organization": {"id": "org_a", "slug": "a"},
            "conversation": {"id": "cv_a"}, "contact": {"identity": "521111111111"},
            "messages": [{"id": "msg_a", "type": "text", "text": "hola"}]}


def cloud():
    return make_ctx(make_settings(vocero_mode="cloud", crm_organization="a", crm_brain_secret="test-brain-secret"))


async def post(client, ctx, data):
    body = json.dumps(data).encode()
    signature = hmac.new(ctx.settings.crm_brain_secret.encode(), body, hashlib.sha256).hexdigest()
    return await client.post("/vocero/dispatch", content=body, headers={"x-vocero-signature": "sha256=" + signature})


async def test_ack_is_durable_without_running_turn(monkeypatch):
    ctx = cloud()
    process = AsyncMock()
    monkeypatch.setattr("app.dispatch._procesar", process)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://test") as client:
        assert (await post(client, ctx, payload())).status_code == 200
        assert len(ctx.store.dispatch_inbox) == 1
        process.assert_not_called()
        assert (await post(client, ctx, payload())).status_code == 200
        assert len(ctx.store.dispatch_inbox) == 1
        assert await drain_one(ctx)
        assert not await drain_one(ctx)
        process.assert_awaited_once()
        # 031 — El webhook de Meta ya no entra: 503 para que Meta reintente.
        assert (await client.post("/webhook", content=b"{}")).status_code == 503
        assert (await client.get("/webhook")).status_code == 404
    await ctx.crm.aclose()


async def test_db_failure_does_not_ack(monkeypatch):
    ctx = cloud()
    monkeypatch.setattr(ctx.store, "enqueue_dispatch", AsyncMock(side_effect=RuntimeError("DB down")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://test") as client:
        assert (await post(client, ctx, payload())).status_code == 503
        assert (await post(client, ctx, {"organization": "malformed"})).status_code == 422
        response = await client.post("/vocero/dispatch", content=b"x" * (2 * 1024 * 1024 + 1))
        assert response.status_code == 413
    await ctx.crm.aclose()


async def test_abandoned_started_turn_only_handoffs(monkeypatch):
    ctx = cloud()
    await ctx.store.enqueue_dispatch("org_a", payload())
    async with ctx.store.claim_dispatch() as job:
        assert job and not job.recovery
        # Simulates death without finish, after any number of external effects.
    row = next(iter(ctx.store.dispatch_inbox.values()))
    row["available_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    process = AsyncMock()
    monkeypatch.setattr("app.dispatch._procesar", process)
    ctx.crm.post_handoff = AsyncMock()
    await drain_one(ctx)
    process.assert_not_called()
    ctx.crm.post_handoff.assert_awaited_once_with("cv_a", "error")
    assert row["state"] == "done"
    await ctx.crm.aclose()


async def test_failed_handoff_remains_recoverable(monkeypatch):
    ctx = cloud()
    await ctx.store.enqueue_dispatch("org_a", payload())
    monkeypatch.setattr("app.dispatch._procesar", AsyncMock(side_effect=RuntimeError("turn failed")))
    ctx.crm.post_handoff = AsyncMock(side_effect=RuntimeError("CRM down"))
    with pytest.raises(RuntimeError):
        await drain_one(ctx)
    assert next(iter(ctx.store.dispatch_inbox.values()))["state"] == "started"
    await ctx.crm.aclose()


async def test_pdf_parser_isolated_valid_and_corrupt():
    import io
    from pypdf import PdfWriter
    pdf = PdfWriter()
    pdf.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    pdf.write(buf)
    assert await _pdf_text(buf.getvalue()) == ""
    assert await _pdf_text(b"not a pdf") is None

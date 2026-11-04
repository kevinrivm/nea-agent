"""/health: qué Nea es (versión, commit, modo) y si Meta le sigue escribiendo.

Lo lee el CRM para su tarjeta «Quién responde». Dos cosas no se negocian:
el código HTTP lo decide solo la base, y en la respuesta no viaja nada secreto.

Desde la 2.0 (spec 031) no hay bloque `relay`: Nea ya no reenvía nada al CRM.
En su lugar, `webhookDeMeta` dice cuándo llegó el último webhook de Meta, que
Nea ya no recibe (app/webhook_retirado.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from app.main import create_app
from app.state import utcnow
from tests.conftest import make_ctx, make_settings


@pytest.fixture(autouse=True)
def _sin_version_en_el_entorno(monkeypatch):
    for var in ("NEA_VERSION", "NEA_BUILD_COMMIT", "SOURCE_COMMIT"):
        monkeypatch.delenv(var, raising=False)


async def _health(ctx) -> tuple[int, dict, str]:
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as client:
        resp = await client.get("/health")
    await ctx.crm.aclose()
    return resp.status_code, resp.json(), resp.text


async def test_por_defecto():
    status, cuerpo, _ = await _health(make_ctx())
    assert status == 200
    assert cuerpo == {
        "status": "ok",
        "db": "ok",
        "version": "dev",
        "mode": "estándar",
        "llm": "propia",
        "webhookDeMeta": {"ultimoEn": None},
    }


async def test_el_commit_del_build_viaja_verificado(monkeypatch):
    monkeypatch.setenv("NEA_VERSION", "2.0.0")
    monkeypatch.setenv("NEA_BUILD_COMMIT", "0123456789abcdef")
    monkeypatch.setenv("SOURCE_COMMIT", "fedcba9876543210")  # el build gana
    _, cuerpo, _ = await _health(make_ctx())
    assert cuerpo["version"] == "2.0.0"
    assert cuerpo["commit"] == "0123456"
    assert cuerpo["commitVerified"] is True


async def test_el_commit_del_entorno_viaja_sin_verificar(monkeypatch):
    """Es la palabra de la plataforma, no de la imagen: puede estar desfasado."""
    monkeypatch.setenv("SOURCE_COMMIT", "abcdef0123")
    _, cuerpo, _ = await _health(make_ctx())
    assert cuerpo["commit"] == "abcdef0"
    assert cuerpo["commitVerified"] is False


async def test_un_webhook_de_meta_sale_en_health_y_no_lo_tumba():
    ctx = make_ctx()
    ctx.webhook_meta_ultimo = utcnow() - timedelta(seconds=30)
    status, cuerpo, _ = await _health(ctx)
    assert status == 200
    visto = datetime.fromisoformat(cuerpo["webhookDeMeta"]["ultimoEn"])
    assert utcnow() - visto < timedelta(minutes=1)


@pytest.mark.parametrize(
    "ajustes,modo",
    [
        ({"vocero_mode": "cloud", "crm_organization": "mi-negocio"}, "cloud"),
        ({"vocero_mode": "cloud", "crm_organization": ""}, "multiorg"),
    ],
)
async def test_en_cloud_dice_su_modo(ajustes, modo):
    _, cuerpo, _ = await _health(make_ctx(make_settings(**ajustes)))
    assert cuerpo["mode"] == modo
    assert "relay" not in cuerpo


async def test_sin_base_es_503_pero_dice_quien_es(monkeypatch):
    ctx = make_ctx()

    async def caida():
        raise OSError("connection refused")

    monkeypatch.setattr(ctx.store, "ping", caida)
    status, cuerpo, _ = await _health(ctx)
    assert status == 503
    assert cuerpo["status"] == "degraded" and cuerpo["db"] == "error"
    assert cuerpo["version"] == "dev" and cuerpo["mode"] == "estándar"


async def test_no_viaja_nada_secreto():
    ctx = make_ctx()
    _, _, texto = await _health(ctx)
    for secreto in (ctx.settings.crm_bot_api_key, ctx.settings.llm_api_key):
        assert secreto not in texto

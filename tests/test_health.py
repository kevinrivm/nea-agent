"""/health: qué Nea es (versión, commit, modo) y cómo va el relay.

Lo leerá el CRM para su tarjeta «Quién responde». Dos cosas no se negocian:
el código HTTP lo decide solo la base (una cola atrasada no debe tumbar el
HEALTHCHECK del contenedor), y en la respuesta no viaja nada secreto.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from app.main import create_app
from app.state import utcnow
from tests.conftest import CRM_WEBHOOK_URL, make_ctx, make_settings


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
        "relay": {"pendientes": 0, "masViejoSegundos": None, "ultimoErrorEn": None},
    }


async def test_el_commit_del_build_viaja_verificado(monkeypatch):
    monkeypatch.setenv("NEA_VERSION", "1.4.0")
    monkeypatch.setenv("NEA_BUILD_COMMIT", "0123456789abcdef")
    monkeypatch.setenv("SOURCE_COMMIT", "fedcba9876543210")  # el build gana
    _, cuerpo, _ = await _health(make_ctx())
    assert cuerpo["version"] == "1.4.0"
    assert cuerpo["commit"] == "0123456"
    assert cuerpo["commitVerified"] is True


async def test_el_commit_del_entorno_viaja_sin_verificar(monkeypatch):
    """Es la palabra de la plataforma, no de la imagen: puede estar desfasado."""
    monkeypatch.setenv("SOURCE_COMMIT", "abcdef0123")
    _, cuerpo, _ = await _health(make_ctx())
    assert cuerpo["commit"] == "abcdef0"
    assert cuerpo["commitVerified"] is False


async def test_una_cola_atrasada_no_tumba_el_healthcheck():
    ctx = make_ctx()
    viejo = await ctx.store.enqueue_relay(b"{}", None)
    await ctx.store.enqueue_relay(b"{}", None)
    entregado = await ctx.store.enqueue_relay(b"{}", None)
    await ctx.store.mark_relay_delivered(entregado)
    ctx.store.relays[viejo].created_at = utcnow() - timedelta(minutes=10)
    await ctx.store.reschedule_relay(viejo, 1, utcnow() + timedelta(seconds=2))

    status, cuerpo, _ = await _health(ctx)

    assert status == 200
    relay = cuerpo["relay"]
    assert relay["pendientes"] == 2
    assert 600 <= relay["masViejoSegundos"] < 700
    error = datetime.fromisoformat(relay["ultimoErrorEn"])
    assert utcnow() - error < timedelta(minutes=1)


async def test_si_la_cola_no_se_puede_leer_sigue_en_200(monkeypatch):
    ctx = make_ctx()

    async def revienta():
        raise RuntimeError("relation relay_queue does not exist")

    monkeypatch.setattr(ctx.store, "relay_stats", revienta)
    status, cuerpo, _ = await _health(ctx)
    assert status == 200
    assert cuerpo["relay"] is None


@pytest.mark.parametrize(
    "ajustes,modo",
    [
        ({"vocero_mode": "cloud", "crm_organization": "mi-negocio"}, "cloud"),
        ({"vocero_mode": "cloud", "crm_organization": ""}, "multiorg"),
    ],
)
async def test_en_cloud_dice_su_modo_y_no_hay_relay(ajustes, modo):
    """En cloud el relay no corre: el CRM ya tiene el mensaje."""
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
    await ctx.store.reschedule_relay(
        await ctx.store.enqueue_relay(b"{}", "sha256=firma"), 1, utcnow()
    )
    _, _, texto = await _health(ctx)
    for secreto in (
        CRM_WEBHOOK_URL,
        "tok-crm",  # el token va en la ruta del webhook del CRM
        ctx.settings.crm_bot_api_key,
        ctx.settings.llm_api_key,
        "sha256=firma",
    ):
        assert secreto not in texto

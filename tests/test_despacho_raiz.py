"""031 — Nea recibe el despacho de Vocero raíz: la única entrada desde la 2.0.

`CUERPO_GEMELO` y `FIRMA_GEMELA` están copiados byte a byte de
`tests/unit/despacho-contrato.test.ts` en vocero-crm. Si el CRM cambia el
cuerpo o cómo lo firma, esa prueba se pone roja allá; si Nea deja de aceptarlo,
esta se pone roja aquí. Cambiar un lado sin el otro rompe una prueba, no una
instalación.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from app.dispatch import mensajes_del_despacho
from app.main import create_app
from tests.conftest import (
    CRM_URL,
    crm_context,
    despachar,
    evento_despacho,
    firmar,
    make_ctx,
    make_settings,
    mock_crm_basics,
    vaciar_cola,
)

SECRETO_GEMELO = "llave-de-prueba-del-contrato-0001"

CUERPO_GEMELO = (
    '{"dispatchId":"dsp_prueba0000000000001","organization":{"id":"org_prueba","slug":"mi-negocio"},'
    '"conversation":{"id":"cv_prueba","channel":"whatsapp","aiEnabled":true,"windowOpen":true,"windowExpiresAt":"2026-10-09T05:10:11.000Z"},'
    '"contact":{"id":"ct_prueba","displayName":"Ana Pérez","identity":"5215512345678"},'
    '"messages":[{"id":"msg_1","at":"2026-10-08T05:10:03.000Z","type":"text","text":"hola, ¿tienen el modelo azul?","mediaId":null},'
    '{"id":"msg_2","at":"2026-10-08T05:10:11.000Z","type":"image","text":null,"mediaId":"1234567890","mimeType":"image/jpeg","caption":"este"}],'
    '"firstMessageAt":"2026-10-08T05:10:03.000Z","lastMessageAt":"2026-10-08T05:10:11.000Z"}'
).encode("utf-8")

FIRMA_GEMELA = "sha256=885074513a6fbd7e2d4443e7f45e3837246effed6c9efa596260057f4dd9008d"


def _cliente(ctx) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    )


async def test_la_firma_gemela_es_la_del_cuerpo_gemelo():
    assert firmar(CUERPO_GEMELO, SECRETO_GEMELO) == FIRMA_GEMELA


async def test_el_cuerpo_gemelo_entra_firmado_con_la_llave_del_bot():
    """AC1/AC2: firmado con `CRM_BOT_API_KEY`, el cuerpo del CRM se acepta tal cual."""
    ctx = make_ctx(make_settings(crm_bot_api_key=SECRETO_GEMELO))
    async with _cliente(ctx) as client:
        resp = await client.post(
            "/vocero/dispatch",
            content=CUERPO_GEMELO,
            headers={"x-vocero-signature": FIRMA_GEMELA},
        )
    assert resp.status_code == 200
    # En la cola durable, como cualquier despacho: se procesa fuera de la ruta.
    assert [*ctx.store.dispatch_inbox] == [("org_prueba", "dsp_prueba0000000000001")]
    await ctx.crm.aclose()


async def test_otra_llave_o_sin_firma_es_401():
    ctx = make_ctx(make_settings(crm_bot_api_key="otra-llave"))
    async with _cliente(ctx) as client:
        con_otra = await client.post(
            "/vocero/dispatch", content=CUERPO_GEMELO, headers={"x-vocero-signature": FIRMA_GEMELA}
        )
        sin_firma = await client.post("/vocero/dispatch", content=CUERPO_GEMELO)
    assert (con_otra.status_code, sin_firma.status_code) == (401, 401)
    assert not getattr(ctx.store, "dispatch_inbox", {})
    await ctx.crm.aclose()


async def test_sin_llave_configurada_no_entra_nada():
    """Una Nea sin `CRM_BOT_API_KEY` no acepta despachos, ni con firma vacía."""
    ctx = make_ctx(make_settings(crm_bot_api_key=""))
    async with _cliente(ctx) as client:
        resp = await client.post(
            "/vocero/dispatch", content=CUERPO_GEMELO, headers={"x-vocero-signature": firmar(CUERPO_GEMELO, "")}
        )
    assert resp.status_code == 401
    await ctx.crm.aclose()


async def test_contesta_por_api_bot_y_pide_el_contexto_por_conversacion(respx_mock):
    ctx = make_ctx()
    routes = mock_crm_basics(respx_mock)
    async with _cliente(ctx) as client:
        assert (await despachar(client, ctx, esperar=True, text="hola")).status_code == 200
    assert routes["messages"].call_count == 1
    assert json.loads(routes["messages"].calls[0].request.content)["conversationId"] == "cv_test1"
    assert routes["context"].calls[0].request.url.params["conversationId"] == "cv_test1"
    await ctx.crm.aclose()


async def test_lo_que_daba_el_webhook_llega_por_el_despacho():
    """AC6: ubicación, tarjeta y nombre del archivo; la voz sale del tipo."""
    evento = evento_despacho(
        mensajes=[
            {"id": "m1", "at": "x", "type": "location", "text": None, "mediaId": None,
             "location": {"latitude": 19.43, "longitude": -99.13, "name": "Zócalo"}},
            {"id": "m2", "at": "x", "type": "contacts", "text": None, "mediaId": None,
             "contacts": [{"name": "Luis Gómez"}, {"name": ""}, "basura"]},
            {"id": "m3", "at": "x", "type": "document", "text": None, "mediaId": "d1",
             "mimeType": "application/pdf", "caption": None, "fileName": "cotizacion.pdf"},
            {"id": "m4", "at": "x", "type": "audio", "text": None, "mediaId": "a1",
             "mimeType": "audio/ogg; codecs=opus", "caption": None},
            {"id": "m5", "at": "x", "type": "audio", "text": None, "mediaId": "a2",
             "mimeType": "audio/mpeg", "caption": None},
            {"id": "m6", "at": "x", "type": "location", "text": None, "mediaId": None},
        ],
        channel="instagram",
    )
    ubicacion, tarjeta, pdf, voz, audio, sin_coordenadas = mensajes_del_despacho(evento)
    assert ubicacion.location == {"latitude": 19.43, "longitude": -99.13, "name": "Zócalo"}
    assert tarjeta.contact_names == ["Luis Gómez"]
    assert pdf.media_filename == "cotizacion.pdf"
    assert (voz.media_voice, audio.media_voice) == (True, False)
    assert sin_coordenadas.location is None
    assert {m.channel for m in (ubicacion, tarjeta, pdf)} == {"instagram"}


async def test_una_ubicacion_llega_al_modelo_con_coordenadas(respx_mock):
    ctx = make_ctx()
    mock_crm_basics(respx_mock)
    async with _cliente(ctx) as client:
        await despachar(client, ctx, esperar=True, msg_type="location")
    usuario = [m["content"] for m in ctx.llm.calls[0]["messages"] if m["role"] == "user"][-1]
    assert "lat 20.69, long -101.36" in usuario
    assert "None" not in usuario
    await ctx.crm.aclose()


async def test_en_instagram_el_prompt_nombra_instagram(respx_mock):
    """AC3: un DM de Instagram no se contesta «por WhatsApp»."""
    ctx = make_ctx()
    mock_crm_basics(respx_mock)
    async with _cliente(ctx) as client:
        await despachar(client, ctx, esperar=True, channel="instagram", frm="ig:178414")
    sistema = ctx.llm.calls[0]["messages"][0]["content"]
    assert "agente de IA de Instagram" in sistema
    assert "WhatsApp" not in sistema.split("CONTEXTO ACTUAL")[0]
    await ctx.crm.aclose()


async def test_sin_canal_en_el_despacho_vale_el_del_contacto(respx_mock):
    ctx = make_ctx()
    contexto = crm_context()
    contexto["contact"]["channel"] = "messenger"
    mock_crm_basics(respx_mock)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(return_value=httpx.Response(200, json=contexto))
    evento = evento_despacho(text="hola")
    del evento["conversation"]["channel"]
    cuerpo = json.dumps(evento).encode()
    async with _cliente(ctx) as client:
        await client.post("/vocero/dispatch", content=cuerpo, headers={"x-vocero-signature": firmar(cuerpo)})
        await vaciar_cola(ctx)
    assert "agente de IA de Messenger" in ctx.llm.calls[0]["messages"][0]["content"]
    await ctx.crm.aclose()


async def test_un_mensaje_repetido_en_dos_despachos_se_lee_una_vez(respx_mock):
    """AC4: la raíz también repite la ráfaga completa."""
    ctx = make_ctx()
    mock_crm_basics(respx_mock)
    primero = {"id": "msg_1", "at": "x", "type": "text", "text": "hola", "mediaId": None}
    segundo = {"id": "msg_2", "at": "x", "type": "text", "text": "¿cuánto cuesta?", "mediaId": None}
    async with _cliente(ctx) as client:
        await despachar(client, ctx, esperar=True, mensajes=[primero])
        await despachar(client, ctx, esperar=True, mensajes=[primero, segundo])
    del_lead = [m.content for m in ctx.store.messages if m.role == "user"]
    assert del_lead == ["hola", "¿cuánto cuesta?"]
    await ctx.crm.aclose()


async def test_el_crm_caido_al_leer_el_contexto_termina_con_un_humano(respx_mock):
    """Sin los reintentos de Nea 1.x: la cola del despacho se la pasa a un humano."""
    ctx = make_ctx()
    routes = mock_crm_basics(respx_mock)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(return_value=httpx.Response(503))
    async with _cliente(ctx) as client:
        await despachar(client, ctx, esperar=True, text="hola")
    assert routes["messages"].call_count == 0
    assert routes["handoff"].call_count == 1
    assert json.loads(routes["handoff"].calls[0].request.content)["reason"] == "error"
    await ctx.crm.aclose()


async def test_un_webhook_de_meta_contesta_503_avisa_y_sale_en_health(caplog):
    """AC7: Meta reintenta; el log dice qué hacer, una vez por minuto."""
    ctx = make_ctx()
    async with _cliente(ctx) as client:
        with caplog.at_level(logging.ERROR, logger="nea.webhook_retirado"):
            primero = await client.post("/webhook", content=b'{"object":"whatsapp_business_account"}')
            segundo = await client.post("/webhook/token-viejo", content=b"{}")
        verificacion = await client.get("/webhook?hub.mode=subscribe&hub.challenge=1")
        salud = (await client.get("/health")).json()
    assert (primero.status_code, segundo.status_code, verificacion.status_code) == (503, 503, 404)
    avisos = [r for r in caplog.records if "devuélvele el webhook al CRM" in r.getMessage()]
    assert len(avisos) == 1
    assert salud["webhookDeMeta"]["ultimoEn"] is not None
    await ctx.crm.aclose()


async def test_las_variables_de_1x_avisan_al_arrancar(monkeypatch, caplog):
    from app import main

    monkeypatch.setenv("META_APP_SECRET", "x")
    monkeypatch.setenv("CRM_WEBHOOK_URL", "https://crm/api/webhooks/wa/tok")
    with caplog.at_level(logging.WARNING, logger="nea.main"):
        main._avisar_obsoletas()
    texto = caplog.text
    assert "META_APP_SECRET" in texto and "CRM_WEBHOOK_URL" in texto
    assert "tok" not in texto  # el nombre de la variable, nunca su valor
    await asyncio.sleep(0)


async def test_una_prueba_del_laboratorio_pasa_la_lista_y_no_programa_seguimiento(respx_mock):
    """033 — `conversation.test`: la identidad es sintética y la respuesta no sale."""
    ctx = make_ctx(make_settings(allowed_wa_ids="5215550009999"))
    routes = mock_crm_basics(respx_mock)
    async with _cliente(ctx) as client:
        await despachar(client, ctx, esperar=True, text="hola", frm="5210000000001", prueba=True)
    assert routes["messages"].call_count == 1
    conv = next(iter(ctx.store.conversations.values()))
    assert conv.followup_due_at is None
    await ctx.crm.aclose()


async def test_una_real_fuera_de_la_lista_sigue_sin_respuesta(respx_mock):
    ctx = make_ctx(make_settings(allowed_wa_ids="5215550009999"))
    routes = mock_crm_basics(respx_mock)
    async with _cliente(ctx) as client:
        await despachar(client, ctx, esperar=True, text="hola", frm="5210000000001")
    assert routes["messages"].call_count == 0
    await ctx.crm.aclose()


async def test_test_que_no_es_true_literal_no_cuenta():
    evento = evento_despacho(text="hola")
    evento["conversation"]["test"] = "true"
    assert mensajes_del_despacho(evento)[0].prueba is False

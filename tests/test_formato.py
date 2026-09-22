"""Markdown → WhatsApp (app/formato.py).

Las muestras están escritas como las escribe GLM cuando se le olvida que
está en WhatsApp: precios en negritas dobles, títulos, tablas, enlaces de
Markdown. Lo que se fija aquí es lo que el lead termina viendo.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from app.followup import FollowupWorker
from app.formato import a_whatsapp
from app.llm import LlmReply
from app.main import create_app
from app.state import utcnow
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    crm_context,
    make_ctx,
    mock_crm_basics,
    wa_body,
)

MUESTRAS = [
    # 1. Precio en negritas dobles, con emoji y acentos.
    (
        "¡Claro! 😊 La **consulta de valoración** cuesta **$800 MXN** e incluye "
        "diagnóstico completo.",
        "¡Claro! 😊 La *consulta de valoración* cuesta *$800 MXN* e incluye "
        "diagnóstico completo.",
    ),
    # 2. Títulos y viñetas con precio: el título queda en negrita de WhatsApp.
    (
        "## Nuestros paquetes\n\n### 🦷 Limpieza dental\n- **Precio:** $600\n"
        "- Duración: 45 min\n\n### ✨ Blanqueamiento\n- **Precio:** $2,500",
        "*Nuestros paquetes*\n\n*🦷 Limpieza dental*\n- *Precio:* $600\n"
        "- Duración: 45 min\n\n*✨ Blanqueamiento*\n- *Precio:* $2,500",
    ),
    # 3. Enlace de Markdown: texto y URL, con la URL intacta (guiones bajos).
    (
        "Puedes agendar aquí: [Agenda tu valoración](https://cal.com/clinica_sonrie/"
        "valoracion__30min) y listo 🙌",
        "Puedes agendar aquí: Agenda tu valoración: https://cal.com/clinica_sonrie/"
        "valoracion__30min y listo 🙌",
    ),
    # 4. Tabla de dos columnas.
    (
        "Te comparto precios:\n\n| Servicio | Precio |\n|----------|--------|\n"
        "| Consulta | $800 |\n| Limpieza | $600 |\n\n¿Cuál te interesa?",
        "Te comparto precios:\n\nConsulta: $800\nLimpieza: $600\n\n¿Cuál te interesa?",
    ),
    # 5. Tabla de tres columnas con alineación y negritas en una celda.
    (
        "| Plan | Precio | Sesiones |\n|:---|:---:|---:|\n| Básico | **$500** | 4 |\n"
        "| Pro | $900 | 8 |",
        "Básico — Precio: *$500*, Sesiones: 4\nPro — Precio: $900, Sesiones: 8",
    ),
    # 6. URL suelta con `_` y correo con `_`: no se tocan.
    (
        "Te dejo el enlace: https://wa.me/5215512345678?text=Hola_quiero_info y el "
        "correo ventas_mx@clinica-sonrie.com.",
        "Te dejo el enlace: https://wa.me/5215512345678?text=Hola_quiero_info y el "
        "correo ventas_mx@clinica-sonrie.com.",
    ),
    # 7. Negritas alrededor de un enlace: se quitan, el enlace manda.
    ("**https://cal.com/kevin_rivera**", "https://cal.com/kevin_rivera"),
    ("**Agenda aquí: https://cal.com/x__y**", "Agenda aquí: https://cal.com/x__y"),
    # 8. Código en línea y cerco: se va el cerco, se queda el contenido.
    (
        "Usa el código `PROMO_2026` al pagar.\n\n```\nTotal: $1,200\n```",
        "Usa el código PROMO_2026 al pagar.\n\nTotal: $1,200",
    ),
    # 9. Tachado doble y renglones vacíos de más.
    (
        "~~$1,000~~ **$800** por tiempo limitado\n\n\n\n¿Te lo aparto?",
        "~$1,000~ *$800* por tiempo limitado\n\n¿Te lo aparto?",
    ),
    # 10. Enlace cuyo texto ES la URL: queda solo la URL.
    ("Más info en [https://clinica.mx](https://clinica.mx)", "Más info en https://clinica.mx"),
    # 11. Negrita + cursiva triple y separador horizontal.
    (
        "***Importante:*** trae tu identificación.\n\n---\n\n**Horario:**\n"
        "Lunes a viernes de 9 a 18 h",
        "*_Importante:_* trae tu identificación.\n\n*Horario:*\n"
        "Lunes a viernes de 9 a 18 h",
    ),
    # 12. mailto: y tel:, con el número ya a la vista.
    (
        "Escríbenos a [soporte](mailto:hola@negocio.mx) o llama al "
        "[55 1234 5678](tel:+525512345678).",
        "Escríbenos a soporte: hola@negocio.mx o llama al 55 1234 5678.",
    ),
    # 13. Subrayado doble como énfasis.
    ("__Nota__: el anticipo es de **$200**.", "_Nota_: el anticipo es de *$200*."),
    # 14. Lista numerada con negritas bajo un título.
    (
        "### Pasos para agendar\n1. **Elige** tu horario\n2. Paga el anticipo de "
        "**$200**\n3. ¡Listo! 🎉",
        "*Pasos para agendar*\n1. *Elige* tu horario\n2. Paga el anticipo de "
        "*$200*\n3. ¡Listo! 🎉",
    ),
]


@pytest.mark.parametrize("entrada,esperado", MUESTRAS)
def test_muestras_realistas(entrada, esperado):
    assert a_whatsapp(entrada) == esperado


@pytest.mark.parametrize("entrada,esperado", MUESTRAS)
def test_idempotente(entrada, esperado):
    """Correr dos veces no cambia nada: el historial ya guarda lo convertido."""
    assert a_whatsapp(esperado) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "Hola, soy *Nea* 👋 ¿en qué te ayudo?",
        "Eso es _muy_ común, no te preocupes.",
        "Antes ~$1,000~, hoy $800.",
        "Tenemos:\n- Consulta\n- Limpieza\n* Blanqueamiento",
        "1. Elige horario\n2. Confirma",
        "Mañana a las 10:00 ¿te acomoda? Atención en Querétaro, Mérida y Cancún.",
        "5 * 3 = 15 y el #1 en la zona #promo",
        "Sígueme en IG: @kevin_rivera_mx",
        "",
    ],
)
def test_lo_que_ya_es_de_whatsapp_no_se_toca(texto):
    assert a_whatsapp(texto) == texto


@pytest.mark.parametrize(
    "url",
    [
        "https://cal.com/clinica_sonrie/valoracion__30min",
        "https://example.com/a**b**c",
        "https://drive.google.com/file/d/1a_B-c/view?usp=sharing",
        "https://es.wikipedia.org/wiki/Odontolog%C3%ADa_(ciencia)",
        "www.clinica_sonrie.mx/promo__verano",
    ],
)
def test_las_urls_salen_intactas(url):
    for texto in (
        f"Aquí está: {url}",
        f"**Link:** {url}.",
        f"[Da clic aquí]({url})",
        f"Revisa `{url}`",
    ):
        salida = a_whatsapp(texto)
        assert url in salida, (texto, salida)
        assert "**" not in salida.replace(url, "")


def test_ningun_doble_asterisco_sobrevive():
    """Ni una negrita partida en dos renglones, ni una sin cerrar."""
    salida = a_whatsapp("**Precio especial\nhoy** y **sin cerrar")
    assert "**" not in salida
    assert "Precio especial" in salida and "sin cerrar" in salida


def test_tabla_de_comparacion_con_esquina_vacia():
    texto = (
        "|  | Básico | Pro |\n|---|---|---|\n| Sesiones | 4 | 8 |\n"
        "| Precio | $500 | $900 |"
    )
    assert a_whatsapp(texto) == (
        "Sesiones — Básico: 4, Pro: 8\nPrecio — Básico: $500, Pro: $900"
    )


def test_barras_sueltas_no_son_tabla():
    texto = "Lunes | Martes | Miércoles\nde 9 a 18 h"
    assert a_whatsapp(texto) == texto


def test_un_enlace_a_whatsapp_no_se_pierde_aunque_el_texto_sea_el_numero():
    salida = a_whatsapp("Escríbenos: [5512345678](https://wa.me/525512345678)")
    assert "https://wa.me/525512345678" in salida


def test_no_quedan_marcadores_internos():
    salida = a_whatsapp("**[Reserva](https://x.mx/a_b)** o https://y.mx")
    assert chr(0xE000) not in salida and chr(0xE001) not in salida
    assert salida == "Reserva: https://x.mx/a_b o https://y.mx"


# ------------------------------------------------- en el turno y el empujón ---

_CRUDO = (
    "## Precios\n\nLa **valoración** cuesta **$800**. Agenda aquí: "
    "[Reservar](https://cal.com/clinica_sonrie/valoracion__30min)"
)
_CONVERTIDO = (
    "*Precios*\n\nLa *valoración* cuesta *$800*. Agenda aquí: "
    "Reservar: https://cal.com/clinica_sonrie/valoracion__30min"
)


async def test_el_turno_envia_y_guarda_lo_convertido(respx_mock):
    ctx = make_ctx()
    ctx.llm.replies = [LlmReply(content=_CRUDO)]
    routes = mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as client:
        await client.post("/webhook", content=wa_body(text="¿cuánto cuesta?"))
        await asyncio.sleep(0.35)
    assert routes["messages"].call_count == 1
    enviado = json.loads(routes["messages"].calls[0].request.content)["text"]
    assert enviado == _CONVERTIDO
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    historial = await ctx.store.recent_messages(conv.id, 10)
    assert historial[-1].role == "assistant"
    assert historial[-1].content == _CONVERTIDO
    await ctx.crm.aclose()


async def test_el_empujon_envia_y_guarda_lo_convertido(respx_mock):
    ctx = make_ctx()
    ctx.llm.replies = [LlmReply(content=_CRUDO)]
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.add_message(conv.id, "user", "me interesa, luego te digo")
    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id=CRM_CONV_ID,
        greeted=True,
        followup_due_at=utcnow() - timedelta(minutes=1),
    )
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    await FollowupWorker(ctx).tick()
    assert messages.call_count == 1
    assert json.loads(messages.calls[0].request.content)["text"] == _CONVERTIDO
    historial = await ctx.store.recent_messages(conv.id, 10)
    assert historial[-1].content == _CONVERTIDO
    await ctx.crm.aclose()

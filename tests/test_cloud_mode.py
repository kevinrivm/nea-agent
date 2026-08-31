"""Modo cloud: Nea detrás de un Vocero multitenant.

Lo que fijan estos tests es la frontera entre los dos modos. Sin
`VOCERO_MODE=cloud` nada de esto se activa y Nea se comporta como siempre —esa
es la garantía que protege a las instalaciones que ya existen—, y con la
bandera cambia la superficie del CRM, la autenticación y por dónde entran los
mensajes.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from app.config import Settings
from app.crm_brains import (
    BrainsCrmClient,
    _aplanar_reserva,
    _perfil_desde_contexto,
)
from app.dispatch import firma_valida, mensajes_del_despacho

CRM_URL = "http://crm.test"
SECRETO = "secreto-del-cerebro-de-prueba"
CONV = "cv_nube1"
IDENTITY = "525550001111"


def firmar(cuerpo: bytes, secreto: str = SECRETO) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


# ── La bandera ────────────────────────────────────────────────────────────


class TestBandera:
    def test_sin_bandera_no_es_cloud(self) -> None:
        # La garantía que protege a toda instalación existente.
        assert Settings(vocero_mode="").cloud_mode is False

    def test_con_la_bandera_si(self) -> None:
        assert Settings(vocero_mode="cloud").cloud_mode is True

    def test_tolera_mayusculas_y_espacios(self) -> None:
        # Copiado de una consola. Que no arranque en el modo que se pidió por
        # un espacio invisible es de las cosas más caras de diagnosticar.
        assert Settings(vocero_mode=" Cloud ").cloud_mode is True

    def test_cualquier_otro_valor_es_el_modo_de_siempre(self) -> None:
        assert Settings(vocero_mode="meta").cloud_mode is False


# ── La firma del despacho ─────────────────────────────────────────────────


class TestFirma:
    def test_valida_el_cuerpo_crudo(self) -> None:
        cuerpo = b'{"dispatchId":"dsp_1"}'
        assert firma_valida(cuerpo, firmar(cuerpo), SECRETO) is True

    def test_no_valida_un_cuerpo_reserializado(self) -> None:
        # El error clásico: firmar el JSON parseado y vuelto a serializar. Un
        # espacio de diferencia y no coincide nunca.
        original = b'{"dispatchId":"dsp_1", "x":1}'
        reserializado = json.dumps(json.loads(original)).encode()
        assert original != reserializado
        assert firma_valida(reserializado, firmar(original), SECRETO) is False

    def test_rechaza_la_firma_de_otro_secreto(self) -> None:
        cuerpo = b"{}"
        assert firma_valida(cuerpo, firmar(cuerpo, "otro"), SECRETO) is False

    def test_sin_secreto_configurado_RECHAZA(self) -> None:
        # Al revés que el webhook de Meta, donde un secreto vacío significa
        # "dev, no verifiques". Aquí el secreto es la única prueba de que quien
        # despacha es el CRM y no cualquiera que conozca la URL.
        cuerpo = b"{}"
        assert firma_valida(cuerpo, firmar(cuerpo), "") is False

    def test_sin_cabecera_rechaza(self) -> None:
        assert firma_valida(b"{}", None, SECRETO) is False

    def test_acepta_la_firma_sin_el_prefijo(self) -> None:
        cuerpo = b"{}"
        crudo = hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()
        assert firma_valida(cuerpo, crudo, SECRETO) is True


# ── El evento del CRM ─────────────────────────────────────────────────────


class TestDespacho:
    def test_traduce_la_rafaga_entera(self) -> None:
        # El CRM ya esperó a que el cliente terminara de escribir y entrega los
        # mensajes juntos: son UNA pregunta, no tres.
        msgs = mensajes_del_despacho(
            {
                "contact": {"identity": IDENTITY, "displayName": "Ana"},
                "messages": [
                    {"id": "msg_1", "type": "text", "text": "hola"},
                    {"id": "msg_2", "type": "text", "text": "¿tienen el azul?"},
                ],
            }
        )
        assert [m.text for m in msgs] == ["hola", "¿tienen el azul?"]
        assert {m.identity for m in msgs} == {IDENTITY}
        assert msgs[0].profile_name == "Ana"

    def test_pasa_los_adjuntos(self) -> None:
        msgs = mensajes_del_despacho(
            {
                "contact": {"identity": IDENTITY},
                "messages": [
                    {
                        "id": "msg_1",
                        "type": "image",
                        "mediaId": "med_1",
                        "mimeType": "image/jpeg",
                        "caption": "así",
                    }
                ],
            }
        )
        assert msgs[0].media_id == "med_1"
        assert msgs[0].media_mime == "image/jpeg"

    def test_sin_identidad_no_devuelve_nada(self) -> None:
        # Sin identidad no hay a quién contestarle. Mejor callar que adivinar.
        assert mensajes_del_despacho({"messages": [{"id": "m", "text": "hola"}]}) == []

    def test_un_mensaje_basura_no_tumba_la_rafaga(self) -> None:
        msgs = mensajes_del_despacho(
            {
                "contact": {"identity": IDENTITY},
                "messages": ["esto no es un mensaje", {"id": "m", "text": "hola"}],
            }
        )
        assert len(msgs) == 1


# ── Traducciones de forma ─────────────────────────────────────────────────


class TestFormas:
    def test_el_perfil_sale_del_contexto(self) -> None:
        # En esta superficie el perfil no tiene endpoint propio: viaja dentro
        # del contexto de la conversación.
        p = _perfil_desde_contexto(
            {
                "agent": {"name": "Nea", "tone": "cercano"},
                "knowledge": [{"question": "¿precio?", "answer": "500"}],
            }
        )
        assert p["profile"]["name"] == "Nea"
        assert "¿precio?" in p["kb"] and "500" in p["kb"]

    def test_sin_conocimiento_el_kb_es_None(self) -> None:
        # None y no "": el prompt distingue "no hay conocimiento" de un bloque
        # vacío, y un string vacío lo haría escribir una sección en blanco.
        assert _perfil_desde_contexto({"agent": {}, "knowledge": []})["kb"] is None

    def test_ignora_bloques_a_medias(self) -> None:
        p = _perfil_desde_contexto(
            {"knowledge": [{"question": "¿precio?"}, {"answer": "suelto"}]}
        )
        assert p["kb"] is None

    def test_la_reserva_se_aplana(self) -> None:
        # El resto de Nea no debe saber por qué superficie entró la respuesta.
        r = _aplanar_reserva(
            {"ok": True, "booking": {"id": "bk_1", "label": "lunes 11:00"}}
        )
        assert r["label"] == "lunes 11:00" and r["id"] == "bk_1"

    def test_aplanar_tolera_una_respuesta_inesperada(self) -> None:
        assert _aplanar_reserva({"ok": True})["ok"] is True


# ── El cliente ────────────────────────────────────────────────────────────


@pytest.fixture
def cliente() -> BrainsCrmClient:
    return BrainsCrmClient(CRM_URL, SECRETO, "mi-negocio")


class TestCliente:
    @respx.mock
    async def test_habla_con_brains_y_autentica_con_el_secreto(
        self, cliente: BrainsCrmClient
    ) -> None:
        ruta = respx.post(f"{CRM_URL}/api/brains/messages").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        await cliente.send_message(CONV, "hola")

        pedido = ruta.calls.last.request
        assert pedido.headers["authorization"] == f"Bearer {SECRETO}"
        assert pedido.headers["x-vocero-organization"] == "mi-negocio"

    @respx.mock
    async def test_el_contexto_va_por_conversacion_no_por_telefono(
        self, cliente: BrainsCrmClient
    ) -> None:
        # `/api/brains/context` no acepta waIdentity: el ámbito de un cerebro
        # es su organización, y buscar por teléfono sería una forma de
        # preguntar por gente que no le corresponde.
        ruta = respx.get(f"{CRM_URL}/api/brains/context").mock(
            return_value=httpx.Response(
                200, json={"conversation": {"id": CONV}, "agent": {"name": "Nea"}}
            )
        )
        cliente.registrar(IDENTITY, CONV)
        ctx = await cliente.get_context(IDENTITY)

        assert ctx is not None
        assert ruta.calls.last.request.url.params["conversationId"] == CONV

    async def test_sin_despacho_previo_no_hay_contexto(
        self, cliente: BrainsCrmClient
    ) -> None:
        # Sin conversación conocida se calla, igual que el camino de siempre
        # cuando el CRM no conoce la identidad. No inventa.
        assert await cliente.get_context("525559999999") is None

    @respx.mock
    async def test_el_perfil_no_gasta_una_llamada_de_mas(
        self, cliente: BrainsCrmClient
    ) -> None:
        ruta = respx.get(f"{CRM_URL}/api/brains/context").mock(
            return_value=httpx.Response(
                200,
                json={
                    "conversation": {"id": CONV},
                    "agent": {"name": "Nea", "tone": "cercano"},
                    "knowledge": [],
                },
            )
        )
        cliente.registrar(IDENTITY, CONV)
        await cliente.get_context(IDENTITY)
        perfil = await cliente.get_profile()

        assert perfil is not None and perfil["profile"]["name"] == "Nea"
        assert ruta.call_count == 1  # el perfil vino con el contexto

    @respx.mock
    async def test_los_huecos_salen_de_la_agenda_del_crm(
        self, cliente: BrainsCrmClient
    ) -> None:
        respx.get(f"{CRM_URL}/api/brains/agenda/slots").mock(
            return_value=httpx.Response(
                200,
                json={
                    "timezone": "America/Mexico_City",
                    "durationMinutes": 30,
                    "slots": [{"startUtc": "2026-09-01T17:00:00Z", "label": "lunes"}],
                },
            )
        )
        slots = await cliente.get_availability(CONV)
        assert slots[0]["label"] == "lunes"

    @respx.mock
    async def test_reservar_devuelve_los_campos_al_ras(
        self, cliente: BrainsCrmClient
    ) -> None:
        respx.post(f"{CRM_URL}/api/brains/agenda/book").mock(
            return_value=httpx.Response(
                201,
                json={
                    "ok": True,
                    "booking": {
                        "id": "bk_1",
                        "label": "lunes 11:00",
                        "meetingLink": "https://meet.test/x",
                        "linkPending": False,
                    },
                },
            )
        )
        r = await cliente.create_booking(CONV, "2026-09-01T17:00:00Z")
        assert r["label"] == "lunes 11:00"
        assert r["meetingLink"] == "https://meet.test/x"

    @respx.mock
    async def test_reprogramar_usa_PATCH_en_la_misma_ruta(
        self, cliente: BrainsCrmClient
    ) -> None:
        # Es la MISMA cita. Si fuera otra ruta parecería que mover crea una
        # segunda, y el cliente acabaría con dos horarios.
        ruta = respx.patch(f"{CRM_URL}/api/brains/agenda/book").mock(
            return_value=httpx.Response(200, json={"ok": True, "booking": {"id": "bk_1"}})
        )
        await cliente.reschedule_booking(CONV, "2026-09-01T18:00:00Z")
        assert ruta.called

    @respx.mock
    async def test_la_agenda_apagada_se_reconoce(
        self, cliente: BrainsCrmClient
    ) -> None:
        respx.get(f"{CRM_URL}/api/brains/agenda/slots").mock(
            return_value=httpx.Response(404)
        )
        assert await cliente.agenda_available() is False

    @respx.mock
    async def test_si_el_crm_no_responde_se_asume_que_SI_agenda(
        self, cliente: BrainsCrmClient
    ) -> None:
        # Prometer menos de lo que hay es tan malo como prometer de más: sin
        # respuesta no se puede concluir que no haya agenda.
        respx.get(f"{CRM_URL}/api/brains/agenda/slots").mock(
            side_effect=httpx.ConnectError("sin red")
        )
        assert await cliente.agenda_available() is True

    async def test_el_reset_de_pruebas_no_revienta(
        self, cliente: BrainsCrmClient
    ) -> None:
        # No existe en esta superficie. Hacerlo estallar convertiría un comando
        # de pruebas en una caída del turno.
        await cliente.post_reset(CONV)

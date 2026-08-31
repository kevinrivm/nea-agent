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


# ── El proveedor del modelo ───────────────────────────────────────────────


class TestProveedor:
    def test_sin_base_url_es_OpenAI_como_siempre(self) -> None:
        # La garantía para quien ya corre Nea: no cambia de proveedor por que
        # esta variable exista.
        assert Settings().llm_base_url == ""

    def test_se_puede_apuntar_a_openrouter(self) -> None:
        s = Settings(llm_base_url="https://openrouter.ai/api/v1")
        assert s.llm_base_url == "https://openrouter.ai/api/v1"

    def test_el_cliente_apunta_al_proveedor_configurado(self) -> None:
        from app.llm import OpenAiLlm

        llm = OpenAiLlm("k", "anthropic/claude-sonnet-4.5",
                        base_url="https://openrouter.ai/api/v1")
        assert "openrouter.ai" in str(llm._client.base_url)

    def test_sin_base_url_el_cliente_va_a_openai(self) -> None:
        from app.llm import OpenAiLlm

        llm = OpenAiLlm("k", "gpt-4o-mini", base_url=None)
        assert "openai.com" in str(llm._client.base_url)


# ── Notas de voz con un proveedor que no es OpenAI ────────────────────────


class TestAudio:
    def test_con_openai_usa_el_endpoint_de_transcripcion(self) -> None:
        from app.llm import OpenAiLlm

        assert OpenAiLlm("k", "gpt-4o-mini")._audio_por_chat is False

    def test_con_proveedor_propio_el_audio_va_por_el_chat(self) -> None:
        # No hay endpoint de transcripción fuera de OpenAI: el audio viaja
        # dentro de un mensaje, a un modelo que acepte audio.
        from app.llm import OpenAiLlm

        llm = OpenAiLlm("k", "m", base_url="https://openrouter.ai/api/v1")
        assert llm._audio_por_chat is True

    def test_el_formato_sale_del_mime(self) -> None:
        from app.llm import _formato_de_audio

        # WhatsApp manda las notas de voz en OGG/Opus.
        assert _formato_de_audio("audio/ogg; codecs=opus") == "ogg"
        assert _formato_de_audio("audio/mpeg") == "mp3"

    def test_un_mime_desconocido_no_frena_el_intento(self) -> None:
        # Mejor intentarlo como ogg que rendirse antes de probar.
        from app.llm import _formato_de_audio

        assert _formato_de_audio("audio/rarisimo") == "ogg"
        assert _formato_de_audio("") == "ogg"

    def test_whisper_con_otro_proveedor_se_detecta_al_arrancar(self) -> None:
        # `whisper-1` no existe fuera de OpenAI: esa combinación hace fallar
        # TODA nota de voz. Se avisa al arrancar, no con un cliente esperando.
        s = Settings(llm_base_url="https://openrouter.ai/api/v1")
        assert s.audio_mal_configurado is True

    def test_bien_configurado_no_avisa(self) -> None:
        s = Settings(
            llm_base_url="https://openrouter.ai/api/v1",
            llm_transcribe_model="google/gemini-2.5-flash",
        )
        assert s.audio_mal_configurado is False

    def test_con_openai_nunca_avisa(self) -> None:
        assert Settings().audio_mal_configurado is False

    @respx.mock
    async def test_transcribe_pidiendo_SOLO_el_texto(self) -> None:
        # Sin esa instrucción, un modelo servicial contesta a lo que dijo el
        # cliente en vez de transcribirlo, y Nea respondería a su propia
        # paráfrasis.
        from app.llm import OpenAiLlm

        ruta = respx.post("https://prov.test/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "hola qué tal"}}
                    ]
                },
            )
        )
        llm = OpenAiLlm("k", "chat-model", transcribe_model="oye-model",
                        base_url="https://prov.test")
        texto = await llm.transcribe(b"RIFF-audio-falso", "audio/ogg")

        assert texto == "hola qué tal"
        enviado = json.loads(ruta.calls.last.request.content)
        assert enviado["model"] == "oye-model"  # el que OYE, no el que conversa
        partes = enviado["messages"][0]["content"]
        assert any(p["type"] == "input_audio" for p in partes)
        assert "SOLO" in partes[0]["text"]


# ── Los nombres de las variables ──────────────────────────────────────────


class TestNombresDeVariables:
    """Se renombraron a LLM_* porque el proveedor no tiene por qué ser OpenAI.

    Los viejos siguen funcionando, y eso se COMPRUEBA: una variable que deja de
    leerse no avisa —el bot arranca y se queda mudo—, así que la compatibilidad
    no puede depender de que alguien se acuerde.
    """

    def test_los_nombres_nuevos(self, monkeypatch) -> None:
        monkeypatch.setenv("LLM_API_KEY", "nueva")
        monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
        monkeypatch.setenv("LLM_MODEL", "z-ai/glm-5.3-flash")
        monkeypatch.setenv("LLM_TRANSCRIBE_MODEL", "google/gemini-2.5-flash")
        s = Settings(_env_file=None)
        assert s.llm_api_key == "nueva"
        assert s.llm_model == "z-ai/glm-5.3-flash"
        assert s.llm_transcribe_model == "google/gemini-2.5-flash"

    def test_los_nombres_VIEJOS_siguen_funcionando(self, monkeypatch) -> None:
        # La garantía para toda instalación que ya corre.
        monkeypatch.setenv("OPENAI_API_KEY", "vieja")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
        s = Settings(_env_file=None)
        assert s.llm_api_key == "vieja"
        assert s.llm_base_url == "https://api.openai.com/v1"
        assert s.llm_model == "gpt-4o-mini"

    def test_el_nombre_nuevo_gana_al_viejo(self, monkeypatch) -> None:
        # Si alguien migra a medias, manda el nuevo: es el que puso a
        # propósito, no el que quedó de antes.
        monkeypatch.setenv("OPENAI_API_KEY", "vieja")
        monkeypatch.setenv("LLM_API_KEY", "nueva")
        assert Settings(_env_file=None).llm_api_key == "nueva"

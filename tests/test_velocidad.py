"""Los ajustes de velocidad: razonamiento al minimo y ruteo por rendimiento.

El razonamiento del modelo se cobra en segundos que el cliente pasa mirando
"escribiendo…". Medido en huaraches-nea contra GLM 5.3 Flash: con razonamiento,
4.4 s y 150 tokens PENSANDO por cada 146 caracteres de respuesta; con
"minimal", 2.0 s y cero. Y la misma llamada tardaba 5.4 s o 19.6 s segun a que
proveedor la ruteara OpenRouter — mas varianza que cualquier arreglo del
prompt.

Lo que estos tests protegen no es la velocidad (eso se mide en vivo), sino que
BAJAR EL TIEMPO NUNCA CUESTE UNA RESPUESTA: aqui pasan los modelos de muchos
miembros, no uno solo elegido por nosotros.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.llm import LlmExhausted, OpenAiLlm

pytestmark = pytest.mark.anyio


def _ok(texto: str = "hola") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": texto}}]}
    )


class TestPorDefecto:
    def test_los_dos_ajustes_vienen_encendidos(self) -> None:
        s = Settings()
        assert s.llm_reasoning_effort == "minimal"
        assert s.llm_provider_sort == "throughput"

    def test_no_hay_tope_de_salida(self) -> None:
        # Un tope convirtio UNA llamada lenta en DOS: el modelo gastaba los
        # tokens razonando, chocaba con la pared y habia que repetirla entera.
        assert not hasattr(Settings(), "llm_max_tokens")


class TestLoQueViajaEnLaLlamada:
    @respx.mock
    async def test_van_en_extra_body_no_en_el_cuerpo_de_openai(self) -> None:
        ruta = respx.post("https://prov.test/chat/completions").mock(
            return_value=_ok()
        )
        llm = OpenAiLlm(
            "k", "m", base_url="https://prov.test",
            reasoning_effort="minimal", provider_sort="throughput",
        )
        await llm.complete([{"role": "user", "content": "hola"}])

        enviado = json.loads(ruta.calls.last.request.content)
        assert enviado["reasoning"] == {"effort": "minimal"}
        assert enviado["provider"] == {"sort": "throughput"}
        # El tope sigue sin existir: que no vuelva por la puerta de atras.
        assert "max_tokens" not in enviado

    @respx.mock
    async def test_vacios_no_se_mandan(self) -> None:
        # Vacio = decide el proveedor. Mandar la clave con "" no es lo mismo.
        ruta = respx.post("https://prov.test/chat/completions").mock(
            return_value=_ok()
        )
        llm = OpenAiLlm("k", "m", base_url="https://prov.test")
        await llm.complete([{"role": "user", "content": "hola"}])

        enviado = json.loads(ruta.calls.last.request.content)
        assert "reasoning" not in enviado
        assert "provider" not in enviado


class TestElSeguro:
    """Un modelo que no los acepte tiene que contestar igual."""

    @respx.mock
    async def test_si_el_proveedor_se_queja_se_apagan_y_contesta(self) -> None:
        llamadas: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            cuerpo = json.loads(request.content)
            llamadas.append(cuerpo)
            if "reasoning" in cuerpo:
                return httpx.Response(
                    400, json={"error": {"message": "Reasoning is not supported"}}
                )
            return _ok("contesto igual")

        respx.post("https://prov.test/chat/completions").mock(side_effect=responder)
        llm = OpenAiLlm(
            "k", "modelo-sin-razonamiento", base_url="https://prov.test",
            reasoning_effort="minimal", provider_sort="throughput",
        )
        reply = await llm.complete([{"role": "user", "content": "hola"}])

        # El cliente NO se rinde: reintenta sin los ajustes y responde.
        assert reply.content == "contesto igual"
        assert "reasoning" in llamadas[0]
        assert "reasoning" not in llamadas[-1]

    @respx.mock
    async def test_apagados_se_quedan_apagados(self) -> None:
        # El soporte del modelo no cambia a media vida del proceso: preguntarlo
        # en cada turno seria pagar un fallo por turno.
        def responder(request: httpx.Request) -> httpx.Response:
            cuerpo = json.loads(request.content)
            if "reasoning" in cuerpo:
                return httpx.Response(
                    400, json={"error": {"message": "Reasoning is not supported"}}
                )
            return _ok()

        ruta = respx.post("https://prov.test/chat/completions").mock(
            side_effect=responder
        )
        llm = OpenAiLlm(
            "k", "m", base_url="https://prov.test", reasoning_effort="minimal"
        )
        await llm.complete([{"role": "user", "content": "una"}])
        antes = len(ruta.calls)
        await llm.complete([{"role": "user", "content": "otra"}])

        # El segundo turno sale limpio al primer intento.
        assert len(ruta.calls) == antes + 1
        assert "reasoning" not in json.loads(ruta.calls.last.request.content)

    @respx.mock
    async def test_un_fallo_que_no_es_de_los_ajustes_no_los_apaga(self) -> None:
        # Una caida de red no dice nada sobre si el modelo razona.
        ruta = respx.post("https://prov.test/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": {"message": "boom"}})
        )
        llm = OpenAiLlm(
            "k", "m", base_url="https://prov.test", reasoning_effort="minimal"
        )
        with pytest.raises(LlmExhausted):
            await llm.complete([{"role": "user", "content": "hola"}])

        assert llm._extras_ok is True
        assert all("reasoning" in json.loads(c.request.content) for c in ruta.calls)

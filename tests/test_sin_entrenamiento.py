"""El candado de privacidad: ningun proveedor que entrene con los datos.

Cada llamada al modelo lleva `provider.data_collection = "deny"`, que le dice
a OpenRouter que no enrute a endpoints que se queden con los datos.

Lo que estos tests protegen es que el candado NO SEA OPCIONAL. Al lado viajan
los ajustes de velocidad (`test_velocidad.py`), que se apagan solos si el
proveedor se queja; si el candado se fuera con ellos, la conversacion acabaria
justo en el proveedor que dijimos que no ibamos a usar -- y nadie se enteraria,
porque la respuesta llegaria bien.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.llm import OpenAiLlm

pytestmark = pytest.mark.anyio


def _ok(texto: str = "hola") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": texto}}]}
    )


class TestConversando:
    @respx.mock
    async def test_viaja_en_cada_llamada(self) -> None:
        ruta = respx.post("https://prov.test/chat/completions").mock(
            return_value=_ok()
        )
        llm = OpenAiLlm("k", "m", base_url="https://prov.test")
        await llm.complete([{"role": "user", "content": "hola"}])

        enviado = json.loads(ruta.calls.last.request.content)
        assert enviado["provider"]["data_collection"] == "deny"

    @respx.mock
    async def test_sobrevive_al_apagon_de_los_ajustes_de_velocidad(self) -> None:
        """El caso que importa: el proveedor se queja y el candado SIGUE."""
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

        assert reply.content == "contesto igual"
        # Se fue el ajuste de velocidad...
        assert "sort" not in llamadas[-1]["provider"]
        # ...y el candado se quedo, en ESA llamada y en todas.
        assert all(c["provider"]["data_collection"] == "deny" for c in llamadas)

    @respx.mock
    async def test_tambien_en_los_turnos_siguientes(self) -> None:
        # `_extras_ok` es pegajoso: una vez apagado, no vuelve. El candado no
        # depende de el, asi que tampoco se va con el.
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
            "k", "m", base_url="https://prov.test", reasoning_effort="minimal",
        )
        await llm.complete([{"role": "user", "content": "uno"}])
        await llm.complete([{"role": "user", "content": "dos"}])

        enviado = json.loads(ruta.calls.last.request.content)
        assert "reasoning" not in enviado
        assert enviado["provider"]["data_collection"] == "deny"


class TestEscuchando:
    @respx.mock
    async def test_la_nota_de_voz_tampoco_entrena_a_nadie(self) -> None:
        """Es la VOZ de una persona: con mas razon que el texto."""
        ruta = respx.post("https://prov.test/chat/completions").mock(
            return_value=_ok("lo que dijo")
        )
        llm = OpenAiLlm(
            "k", "m", base_url="https://prov.test",
            transcribe_model="google/gemini-2.5-flash",
        )
        texto = await llm.transcribe(b"\x00\x01", "audio/ogg")

        assert texto == "lo que dijo"
        enviado = json.loads(ruta.calls.last.request.content)
        assert enviado["provider"]["data_collection"] == "deny"

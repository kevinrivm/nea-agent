"""Capa LLM: OpenAI chat.completions con tool-calling y extracción tolerante.

Gotchas del brief que se honran aquí:
- `content` vacío con tool_calls es NORMAL (turno solo-herramientas).
- Respuesta vacía de verdad (sin content ni tool_calls) o excepción → reintento
  con backoff (2 reintentos). Agotado → `LlmExhausted` y el turno degrada en
  silencio + handoff error (Constitución IV).
- Los `arguments` de las tools pueden venir malformados: JSON inválido → {}.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI

logger = logging.getLogger("nea.llm")


class LlmExhausted(Exception):
    """El LLM falló todos los reintentos — el turno debe degradar en silencio."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class Llm(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply: ...

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str: ...


# Los proveedores compatibles piden el formato aparte del binario. WhatsApp
# manda notas de voz en OGG/Opus; el resto se deduce del mime y, si no se
# reconoce, se manda como ogg en vez de fallar antes de intentarlo.
_FORMATOS = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/aac": "aac",
}


def _formato_de_audio(mime: str) -> str:
    return _FORMATOS.get((mime or "").split(";")[0].strip().lower(), "ogg")


class OpenAiLlm:
    RETRIES = 2  # además del intento inicial

    def __init__(
        self,
        api_key: str,
        model: str,
        transcribe_model: str = "whisper-1",
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        # base_url ≠ None → proveedor OpenAI-compatible (p. ej. OpenRouter,
        # para el bench de modelos del 002). Ojo: la transcripción de audio
        # es una API de OpenAI — con otro proveedor degrada honesta
        # (LlmExhausted → fallback), por eso el bench alterno cubre texto.
        # `default_headers` lo usa el modo multi-organización: cuando quien
        # piensa es el CRM (y no OpenRouter directo), hay que decirle de qué
        # organización es cada llamada.
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, default_headers=default_headers
        )
        self._model = model
        self._transcribe_model = transcribe_model
        # Con proveedor propio (OpenRouter y compañía) no hay endpoint de
        # transcripción: el audio se manda DENTRO del chat, a un modelo que
        # sepa oír. Se decide aquí y no en cada llamada para que el camino sea
        # el mismo durante toda la vida del proceso.
        self._audio_por_chat = base_url is not None
        # Contadores de uso (para el bench de costos del 002): tokens reales
        # reportados por el proveedor, acumulados por instancia.
        self.usage = {"prompt": 0, "cached": 0, "completion": 0, "llamadas": 0}

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str:
        """Audio → texto. Vacío o fallo → LlmExhausted.

        Dos caminos, porque los proveedores no ofrecen lo mismo:

        - **OpenAI**: endpoint propio de transcripción (whisper).
        - **Compatible** (OpenRouter…): no existe ese endpoint. El audio va
          codificado DENTRO de un mensaje de chat, a un modelo que acepte
          audio. Ojo: el modelo que conversa y el que oye no tienen por qué
          ser el mismo — hoy los GLM, por ejemplo, no oyen.
        """
        # Sin modelo que oiga no se intenta siquiera. Mandar el audio al que
        # conversa devolvería una alucinación con pinta de transcripción, que
        # es peor que decir que no se pudo: quien la lee no sabe que es falsa.
        if not self._transcribe_model:
            raise LlmExhausted(
                "sin modelo de transcripción configurado — no se transcribe"
            )
        if self._audio_por_chat:
            return await self._transcribir_por_chat(data, mime)
        last_error: Exception | None = None
        content_type = (mime or "audio/ogg").split(";")[0].strip()
        for attempt in range(2):
            try:
                resp = await self._client.audio.transcriptions.create(
                    model=self._transcribe_model,
                    file=(filename, data, content_type),
                    language="es",
                )
                text = (getattr(resp, "text", None) or "").strip()
                if text:
                    return text
                last_error = ValueError("transcripción vacía")
                logger.warning("transcribe: texto vacío, intento %d", attempt + 1)
            except Exception as exc:
                last_error = exc
                logger.warning("transcribe: fallo en intento %d: %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise LlmExhausted(str(last_error))

    async def _transcribir_por_chat(self, data: bytes, mime: str) -> str:
        """El audio, en base64, dentro de un mensaje de chat.

        Es como transcriben los proveedores compatibles: no hay endpoint de
        audio, hay modelos que oyen. Se le pide el texto y NADA más — sin esa
        instrucción, un modelo servicial contesta a lo que dijo el cliente en
        vez de transcribirlo, y Nea acabaría respondiendo a su propia
        paráfrasis.
        """
        fmt = _formato_de_audio(mime)
        b64 = base64.b64encode(data).decode()
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                resp = await self._client.chat.completions.create(
                    model=self._transcribe_model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Transcribe este audio al español. "
                                        "Responde SOLO con la transcripción "
                                        "literal, sin comillas, sin comentarios "
                                        "y sin responder a lo que dice."
                                    ),
                                },
                                {
                                    "type": "input_audio",
                                    "input_audio": {"data": b64, "format": fmt},
                                },
                            ],
                        }
                    ],
                )
                text = (resp.choices[0].message.content or "").strip()
                if text:
                    return text
                last_error = ValueError("transcripción vacía")
                logger.warning("transcribe(chat): vacío, intento %d", attempt + 1)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "transcribe(chat) con %s: fallo en intento %d: %s",
                    self._transcribe_model,
                    attempt + 1,
                    exc,
                )
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise LlmExhausted(str(last_error))

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply:
        last_error: Exception | None = None
        for attempt in range(self.RETRIES + 1):
            try:
                kwargs: dict[str, Any] = {}
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                resp = await self._client.chat.completions.create(
                    model=self._model, messages=messages, **kwargs
                )
                u = getattr(resp, "usage", None)
                if u is not None:
                    det = getattr(u, "prompt_tokens_details", None)
                    self.usage["llamadas"] += 1
                    self.usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
                    self.usage["completion"] += getattr(u, "completion_tokens", 0) or 0
                    self.usage["cached"] += getattr(det, "cached_tokens", 0) or 0
                reply = self._parse(resp)
                if reply.content or reply.tool_calls:
                    return reply
                last_error = ValueError("respuesta vacía del LLM (sin content ni tools)")
                logger.warning("llm: respuesta vacía, intento %d", attempt + 1)
            except Exception as exc:  # red, API, parseo — todo reintenta
                last_error = exc
                logger.warning("llm: fallo en intento %d: %s", attempt + 1, exc)
            if attempt < self.RETRIES:
                await asyncio.sleep(2**attempt)  # 1 s, 2 s
        raise LlmExhausted(str(last_error))

    @staticmethod
    def _parse(resp: Any) -> LlmReply:
        """Extracción tolerante: nunca truena por formato inesperado."""
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return LlmReply(content=None)
        message = getattr(choices[0], "message", None)
        if message is None:
            return LlmReply(content=None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            content = content.strip() or None
        else:
            content = None
        tool_calls: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name:
                continue
            raw_args = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except (TypeError, ValueError):
                logger.warning("llm: arguments malformados en %s — uso {}", name)
                args = {}
            tool_calls.append(
                ToolCall(id=getattr(tc, "id", "") or "", name=name, arguments=args)
            )
        return LlmReply(content=content, tool_calls=tool_calls)

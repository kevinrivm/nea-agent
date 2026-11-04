"""Procesamiento de multimedia entrante (spec 002, US1).

Cada tipo se convierte en contenido para el turno: texto (transcripción,
contenido del PDF, marcadores) y, para imágenes, una parte visual que va al
modelo. Regla dura: cualquier fallo de descarga/transcripción produce un
marcador HONESTO — jamás rompe el turno ni finge haber entendido.

Los marcadores van entre corchetes; el system prompt le explica a Nea cómo
usarlos sin exponer nada técnico al lead.
"""
from __future__ import annotations

import base64
import asyncio
import logging
import os
import sys
from pathlib import Path

from dataclasses import dataclass

from app.state import AppContext, InboundMessage

logger = logging.getLogger("nea.media")

MAX_MEDIA_BYTES = 16 * 1024 * 1024  # límite de adjuntos de la Cloud API
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # tope razonable para visión
PDF_MAX_PAGES = 10
PDF_MAX_CHARS = 8000

_AUDIO_EXT = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/wav": "wav",
}


@dataclass
class MediaPart:
    """Render de un ítem multimedia para el turno."""

    text: str | None = None
    image_data_uri: str | None = None


def _caption_line(msg: InboundMessage, junto_a: str) -> str:
    if msg.media_caption:
        return f' Nota del lead junto a {junto_a}: "{msg.media_caption}".'
    return ""


def _honest_failure(msg: InboundMessage) -> MediaPart:
    nombres = {
        "audio": "una nota de voz",
        "image": "una imagen",
        "document": "un documento",
        "video": "un video",
        "sticker": "un sticker",
    }
    que = nombres.get(msg.type, "contenido")
    return MediaPart(
        text=(
            f"[El lead mandó {que} que NO pudiste abrir/procesar."
            f"{_caption_line(msg, 'ese contenido')} Sé honesta: dile que no lo"
            " pudiste abrir y pídele el contenido en texto o nota de voz.]"
        )
    )


async def _audio(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    if not msg.media_id:
        return _honest_failure(msg)
    data, mime = await ctx.crm.get_media(msg.media_id)
    if len(data) > MAX_MEDIA_BYTES:
        return _honest_failure(msg)
    mime_clean = (mime or msg.media_mime or "audio/ogg").split(";")[0].strip()
    ext = _AUDIO_EXT.get(mime_clean, "ogg")
    transcript = await ctx.llm.transcribe(
        data, mime_clean, filename=f"nota-de-voz.{ext}"
    )
    kind = "Nota de voz" if msg.media_voice else "Audio"
    return MediaPart(text=f"[{kind} del lead, transcrita]: {transcript}")


async def _image(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    if not msg.media_id:
        return _honest_failure(msg)
    data, mime = await ctx.crm.get_media(msg.media_id)
    caption = _caption_line(msg, "la imagen")
    if len(data) > MAX_IMAGE_BYTES:
        return MediaPart(
            text=(
                "[El lead mandó una imagen demasiado pesada para verla."
                f"{caption} Sé honesta si hace falta.]"
            )
        )
    mime_clean = (mime or msg.media_mime or "image/jpeg").split(";")[0].strip()
    uri = f"data:{mime_clean};base64,{base64.b64encode(data).decode()}"
    return MediaPart(
        text=f"[El lead mandó una imagen — la tienes adjunta, puedes verla.{caption}]",
        image_data_uri=uri,
    )


_pdf_slots = asyncio.Semaphore(2)


async def _pdf_text(data: bytes) -> str | None:
    if len(data) > MAX_MEDIA_BYTES:
        return None
    # The timeout includes queueing: excess load cannot accumulate parser jobs.
    async with asyncio.timeout(6):
        async with _pdf_slots:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).with_name("pdf_extract.py")),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
            )
            try:
                stdout, _ = await process.communicate(data)
                if process.returncode != 0:
                    return None
                return stdout[:32000].decode("utf-8", errors="replace")[:PDF_MAX_CHARS]
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()


async def _document(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    filename = msg.media_filename or "documento"
    if not msg.media_id:
        return _honest_failure(msg)
    data, mime = await ctx.crm.get_media(msg.media_id)
    if len(data) > MAX_MEDIA_BYTES:
        return _honest_failure(msg)
    mime_clean = (mime or msg.media_mime or "").split(";")[0].strip()
    text: str | None = None
    if mime_clean == "application/pdf" or filename.lower().endswith(".pdf"):
        text = await _pdf_text(data)
    elif mime_clean.startswith("text/"):
        text = data.decode("utf-8", errors="replace")
    if not text or not text.strip():
        return MediaPart(
            text=(
                f"[El lead mandó el documento '{filename}' pero no pudiste leer"
                f" su contenido.{_caption_line(msg, 'el documento')} Sé honesta"
                " y pídele lo importante en texto.]"
            )
        )
    return MediaPart(
        text=(
            f"[Documento '{filename}' del lead — contenido extraído]:"
            f"{_caption_line(msg, 'el documento')}\n{text.strip()[:PDF_MAX_CHARS]}"
        )
    )


async def _sticker(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    return MediaPart(
        text=(
            "[El lead mandó un sticker — tómalo como gesto/emoción y sigue la"
            " conversación natural; no expliques nada técnico.]"
        )
    )


async def _location(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    loc = msg.location or {}
    detalles = ", ".join(
        str(loc[k]) for k in ("name", "address") if loc.get(k)
    )
    coords = f"lat {loc.get('latitude')}, long {loc.get('longitude')}"
    prefix = f"{detalles} — " if detalles else ""
    return MediaPart(
        text=(
            f"[El lead compartió su ubicación: {prefix}{coords}. Si te dice de"
            " dónde es o ya lo sabes, guarda la zona en la ficha (geo).]"
        )
    )


async def _contacts(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    nombres = ", ".join(msg.contact_names) or "alguien"
    return MediaPart(
        text=(
            f"[El lead compartió una tarjeta de contacto de: {nombres}."
            " Agradécelo y pregunta qué relación tiene con su negocio si aporta.]"
        )
    )


async def _video(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    return MediaPart(
        text=(
            "[El lead mandó un video — NO puedes ver videos todavía."
            f"{_caption_line(msg, 'el video')} Sé honesta y ofrécele que te lo"
            " cuente en texto o con una nota de voz (esas sí las entiendes).]"
        )
    )


async def _fallback(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    return MediaPart(
        text=(
            f"[El lead mandó contenido que no puedes ver (tipo: {msg.type})."
            " Sé honesta: pídele que te lo mande como texto o nota de voz.]"
        )
    )


_HANDLERS = {
    "audio": _audio,
    "image": _image,
    "document": _document,
    "sticker": _sticker,
    "location": _location,
    "contacts": _contacts,
    "video": _video,
    "unsupported": _fallback,
}


async def describe_item(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    """Convierte un ítem no-texto en contenido del turno. Nunca lanza."""
    handler = _HANDLERS.get(msg.type, _fallback)
    try:
        return await handler(ctx, msg)
    except Exception:
        logger.exception(
            "media %s (id=%s) no se pudo procesar — fallback honesto",
            msg.type,
            msg.media_id,
        )
        return _honest_failure(msg)

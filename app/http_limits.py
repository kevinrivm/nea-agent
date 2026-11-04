"""Limits apply to streamed bytes, not to the untrusted Content-Length."""
from fastapi import HTTPException, Request

MAX_EVENT_BYTES = 2 * 1024 * 1024


async def limited_body(request: Request, limit: int = MAX_EVENT_BYTES) -> bytes:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(413, "cuerpo demasiado grande")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "cuerpo demasiado grande")
        body.extend(chunk)
    return bytes(body)

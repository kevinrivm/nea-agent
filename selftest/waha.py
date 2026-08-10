"""Cliente WAHA para certificar Nea sobre el canal real.

WAHA representa únicamente a una línea tester propia. Las guardas viven aquí:
destino fijo, pausa mínima, presupuesto por corrida y kill-switch de archivo.
La key debe estar limitada a una sola sesión con permisos ``read + send``.
"""
from __future__ import annotations

import base64
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
KILL_SWITCH = REPO_ROOT / "STOP_LIVE_RUN"
ENV_CANDIDATES = (REPO_ROOT / ".env", REPO_ROOT.parent / ".env")

MIN_PAUSE_SECONDS = 8.0
MAX_MESSAGES_PER_RUN = 40


class LiveRunAborted(RuntimeError):
    """Kill-switch activado: no se envía nada más."""


class BudgetExhausted(RuntimeError):
    """Se alcanzó el máximo seguro de mensajes de la corrida."""


def load_env() -> dict[str, str]:
    vals: dict[str, str] = {}
    for candidate in ENV_CANDIDATES:
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                vals.setdefault(key.strip(), value.strip())
    return vals


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


@dataclass
class WahaClient:
    base_url: str
    session: str
    api_key: str
    target: str
    sent: int = 0
    started_at: int = field(default_factory=lambda: int(time.time()))
    _last_send: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.target = _digits(self.target)
        if len(self.target) < 8:
            raise ValueError("LIVE_TARGET_NUMBER debe incluir código de país")

    @classmethod
    def from_env(cls) -> "WahaClient":
        env = load_env()
        values = {
            "WAHA_BASE_URL": env.get("WAHA_BASE_URL", "").rstrip("/"),
            "WAHA_SESSION": env.get("WAHA_SESSION", ""),
            "WAHA_API_KEY": env.get("WAHA_API_KEY", ""),
            "LIVE_TARGET_NUMBER": env.get("LIVE_TARGET_NUMBER", ""),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"faltan variables en .env: {', '.join(missing)}")
        return cls(
            base_url=values["WAHA_BASE_URL"],
            session=values["WAHA_SESSION"],
            api_key=values["WAHA_API_KEY"],
            target=values["LIVE_TARGET_NUMBER"],
        )

    @property
    def chat_id(self) -> str:
        return f"{self.target}@c.us"

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers={"X-Api-Key": self.api_key},
            json=payload,
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _guard(self) -> None:
        if KILL_SWITCH.exists():
            raise LiveRunAborted(f"existe {KILL_SWITCH} — corrida frenada")
        if self.sent >= MAX_MESSAGES_PER_RUN:
            raise BudgetExhausted(
                f"tope de {MAX_MESSAGES_PER_RUN} mensajes alcanzado"
            )
        remaining = MIN_PAUSE_SECONDS - (time.monotonic() - self._last_send)
        if remaining > 0:
            time.sleep(remaining)

    def _sent_ok(self) -> None:
        self.sent += 1
        self._last_send = time.monotonic()

    def send_text(self, text: str) -> dict[str, Any]:
        self._guard()
        data = self._request(
            "POST",
            "/api/sendText",
            payload={
                "session": self.session,
                "chatId": self.chat_id,
                "text": text,
            },
        )
        self._sent_ok()
        return dict(data)

    def send_voice(self, path: Path) -> dict[str, Any]:
        self._guard()
        mimetype = mimetypes.guess_type(path.name)[0] or "audio/ogg"
        data = self._request(
            "POST",
            "/api/sendVoice",
            payload={
                "session": self.session,
                "chatId": self.chat_id,
                "file": {
                    "mimetype": mimetype,
                    "filename": path.name,
                    "data": base64.b64encode(path.read_bytes()).decode(),
                },
                "convert": True,
            },
        )
        self._sent_ok()
        return dict(data)

    def send_media(
        self,
        path: Path,
        mediatype: str,
        caption: str | None = None,
        mimetype: str | None = None,
    ) -> dict[str, Any]:
        endpoint = {
            "image": "/api/sendImage",
            "video": "/api/sendVideo",
            "document": "/api/sendFile",
        }.get(mediatype)
        if endpoint is None:
            raise ValueError("mediatype debe ser image, video o document")
        self._guard()
        data = self._request(
            "POST",
            endpoint,
            payload={
                "session": self.session,
                "chatId": self.chat_id,
                "caption": caption,
                "file": {
                    "mimetype": mimetype
                    or mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    "filename": path.name,
                    "data": base64.b64encode(path.read_bytes()).decode(),
                },
            },
        )
        self._sent_ok()
        return dict(data)

    def connection_state(self) -> str:
        data = self._request(
            "GET", f"/api/sessions/{quote(self.session, safe='')}"
        )
        return str(data.get("status") or "UNKNOWN")

    def assert_open(self) -> None:
        state = self.connection_state()
        if state != "WORKING":
            raise RuntimeError(
                f"sesión WAHA '{self.session}' no está conectada (status={state})"
            )

    def find_messages(self, limit: int = 25) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"/api/{quote(self.session, safe='')}/chats/{quote(self.chat_id, safe='')}/messages",
            params={
                "limit": max(1, min(limit, 100)),
                "downloadMedia": "false",
                "filter.timestamp.gte": self.started_at,
            },
        )
        return list(data) if isinstance(data, list) else []

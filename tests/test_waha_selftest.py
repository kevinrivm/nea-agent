from pathlib import Path

import httpx
import pytest
import respx

import selftest.waha as waha


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> waha.WahaClient:
    monkeypatch.setattr(waha, "KILL_SWITCH", tmp_path / "STOP_LIVE_RUN")
    monkeypatch.setattr(waha, "MIN_PAUSE_SECONDS", 0)
    return waha.WahaClient(
        base_url="https://waha.test",
        session="demo-nea-pilot",
        api_key="scoped-secret",
        target="+58 412 345 6789",
        started_at=1_800_000_000,
    )


@respx.mock
def test_send_text_usa_sesion_y_destino_fijos(client: waha.WahaClient) -> None:
    route = respx.post("https://waha.test/api/sendText").mock(
        return_value=httpx.Response(200, json={"id": "msg-1"})
    )
    assert client.send_text("hola") == {"id": "msg-1"}
    assert route.calls[0].request.headers["X-Api-Key"] == "scoped-secret"
    assert route.calls[0].request.content == (
        b'{"session":"demo-nea-pilot","chatId":"584123456789@c.us","text":"hola"}'
    )


@respx.mock
def test_lee_solo_el_chat_objetivo_desde_inicio(client: waha.WahaClient) -> None:
    route = respx.get(
        "https://waha.test/api/demo-nea-pilot/chats/584123456789%40c.us/messages"
    ).mock(return_value=httpx.Response(200, json=[{"body": "respuesta"}]))
    assert client.find_messages() == [{"body": "respuesta"}]
    query = route.calls[0].request.url.params
    assert query["filter.timestamp.gte"] == "1800000000"
    assert query["downloadMedia"] == "false"


def test_presupuesto_y_kill_switch(client: waha.WahaClient) -> None:
    client.sent = waha.MAX_MESSAGES_PER_RUN
    with pytest.raises(waha.BudgetExhausted):
        client.send_text("no sale")

    client.sent = 0
    waha.KILL_SWITCH.touch()
    with pytest.raises(waha.LiveRunAborted):
        client.send_text("tampoco sale")

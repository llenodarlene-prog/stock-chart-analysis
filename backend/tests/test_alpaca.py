import httpx
import pytest

from app.alpaca import AlpacaClient, AlpacaError
from app.config import Settings

SETTINGS = Settings("KEY_ID_SECRET_TEST", "SECRET_VALUE_TEST", "iex", "https://data.example", "https://trade.example", ":memory:")


def bar(t):
    return {"t": t, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "n": 1, "vw": 1.2}


def test_follows_pagination_and_sends_feed_and_headers():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["APCA-API-KEY-ID"] == "KEY_ID_SECRET_TEST"
        assert request.url.params["feed"] == "iex" and request.url.params["adjustment"] == "raw"
        if "page_token" not in request.url.params:
            return httpx.Response(200, json={"bars": {"AIOK": [bar("2026-10-26T13:30:00Z")]}, "next_page_token": "abc"})
        return httpx.Response(200, json={"bars": {"AIOK": [bar("2026-10-26T13:31:00Z")]}, "next_page_token": None})

    client = AlpacaClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    bars = client.bars_1min("AIOK", 0, 100)
    assert len(bars) == 2 and len(calls) == 2 and calls[1].url.params["page_token"] == "abc"


def test_retries_rate_limit_then_succeeds():
    state = {"n": 0}
    waits = []

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "0"}, json={"message": "too many"})
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    client = AlpacaClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=waits.append)
    assert client.bars_1min("AIOK", 0, 100) == []
    assert state["n"] == 2 and waits


def test_errors_are_explicit_and_never_contain_credentials():
    client = AlpacaClient(SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})), sleep=lambda s: None)
    with pytest.raises(AlpacaError) as err:
        client.bars_1min("AIOK", 0, 100)
    text = str(err.value)
    assert "403" in text and "KEY_ID_SECRET_TEST" not in text and "SECRET_VALUE_TEST" not in text


def test_missing_credentials_raise_before_any_request():
    empty = Settings("", "", "iex", "https://data.example", "https://trade.example", ":memory:")
    client = AlpacaClient(empty, transport=httpx.MockTransport(lambda r: pytest.fail("no request expected")))
    with pytest.raises(AlpacaError, match="credentials are missing"):
        client.bars_1min("AIOK", 0, 100)

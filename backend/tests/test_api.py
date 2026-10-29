"""End-to-end API test with the Alpaca HTTP layer replaced by a mock
transport. Fixture bars exist only in this test."""
import hashlib
import json
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app import main
from app.alpaca import AlpacaClient
from app.config import Settings
from app.store import Store
from tests.helpers import ny

SETTINGS = Settings("k", "s", "iex", "https://data.example", "https://trade.example", ":memory:")


def fixture_bars():
    out = []
    price = 13.0
    for day in (20, 21, 22, 23, 26):
        start = ny(2026, 10, day, 9, 30)
        for m in range(390):
            price += 0.003 if (m // 30) % 2 == 0 else -0.002
            t = datetime.fromtimestamp(start + 60 * m, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append({"t": t, "o": round(price, 4), "h": round(price + 0.02, 4), "l": round(price - 0.02, 4), "c": round(price + 0.001, 4), "v": 100 + m % 50, "n": 3, "vw": price})
    return out


def handler(request):
    if request.url.path == "/v1/corporate-actions":
        return httpx.Response(200, json={"corporate_actions": {}, "next_page_token": None})
    bars = fixture_bars()
    start, end = request.url.params["start"], request.url.params["end"]
    chosen = [b for b in bars if start <= b["t"] <= end]
    return httpx.Response(200, json={"bars": {"AIOK": chosen}, "next_page_token": None})


def setup_module():
    main.settings = SETTINGS
    main.store = Store(":memory:")
    main.client = AlpacaClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_chart_and_calculations_share_identical_candles(monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: ny(2026, 10, 26, 15, 0))
    api = TestClient(main.app)
    data = api.get("/api/candles", params={"symbol": "AIOK", "timeframe": "5Min", "sessions": "regular"}).json()
    candles = data["candles"]
    assert data["feed"] == "iex" and data["error"] is None and len(candles) > 300
    for series in data["indicators"].values():
        assert len(series) == len(candles)
    canonical = [[c["time"], f"{c['open']:.6f}", f"{c['high']:.6f}", f"{c['low']:.6f}", f"{c['close']:.6f}", c["volume"], c["complete"]] for c in candles]
    assert hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest() == data["data_hash"]
    # The forming candle at 15:00 is drawn but excluded from signals.
    assert candles[-1]["complete"] is False
    assert all(s["index"] < len(candles) - 1 for s in data["signals"])
    assert data["assessment"]["label"] in ("bullish", "neutral", "bearish")
    assert "probability" not in json.dumps(data["assessment"]).lower().replace("carry no probability", "")


def test_api_errors_are_shown_not_replaced(monkeypatch):
    failing = AlpacaClient(SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "forbidden"})), sleep=lambda s: None)
    monkeypatch.setattr(main, "client", failing)
    monkeypatch.setattr(main, "store", Store(":memory:"))
    data = TestClient(main.app).get("/api/candles", params={"symbol": "ZZZZ", "timeframe": "5Min"}).json()
    assert "401" in data["error"] and data["candles"] == []
    assert not data["gates"]["actionable"]


def test_no_order_endpoints_exist():
    paths = {route.path for route in main.app.routes}
    assert not any("order" in p for p in paths)


def test_plain_next_steps_use_only_existing_levels(monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: ny(2026, 10, 26, 15, 0))
    data = TestClient(main.app).get("/api/candles", params={"symbol": "AIOK", "timeframe": "5Min"}).json()
    plain = data["assessment"]["next_steps"]
    assert plain["headline"] and plain["steps"]
    levels = data["assessment"]["levels"]
    allowed = {levels["resistance"], levels["support"], levels["measured_move_up"], *levels["swing_highs_above"]}
    if levels["resistance"] is not None:
        allowed.add(levels["resistance"] + levels["buffer"])
    for key in ("buy_trigger", "stop"):
        assert plain["levels"][key] is None or plain["levels"][key] in allowed or any(abs(plain["levels"][key] - s["level"]) < 1e-9 for s in data["signals"])
    assert "probab" not in " ".join(plain["steps"]).lower()


def test_scenarios_report_measured_history_not_invented_odds(monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: ny(2026, 10, 26, 15, 0))
    data = TestClient(main.app).get("/api/candles", params={"symbol": "AIOK", "timeframe": "5Min"}).json()
    scenarios = data["assessment"]["next_steps"]["scenarios"]
    assert [s["name"] for s in scenarios][0] in ("Breakout up", "Stays in range")
    lines = [e["history"] for s in scenarios for e in s["entries"] if e["history"]]
    assert lines and all(l.startswith("Past results on this chart") for l in lines)
    assert all(("Too few" in l) or ("no past" in l) or ("not a prediction" in l) for l in lines)

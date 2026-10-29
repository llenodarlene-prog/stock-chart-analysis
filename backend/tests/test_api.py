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
    if request.url.path == "/v2/calendar":
        return httpx.Response(200, json=[])
    bars = fixture_bars()
    if request.url.params.get("timeframe") == "30Min":
        bars = [b for b in bars if b["t"][14:16] in ("00", "30")]
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


def test_alerts_are_created_once_and_plan_stop_alerts(monkeypatch):
    main.store = Store(":memory:")
    data = {"symbol": "AIOK", "timeframe": "5Min", "gates": {"actionable": True},
            "signals": [{"kind": "breakout", "variant": "confirmed", "direction": "bullish", "time": 100, "level": 14.0, "actionable": True, "blocked_reasons": [], "explanation": "Confirmed."},
                        {"kind": "breakout", "variant": "confirmed", "direction": "bullish", "time": 50, "level": 13.0, "actionable": False, "blocked_reasons": ["Historical signal (replay only)."], "explanation": "Old."}],
            "assessment": {"as_of": 100, "evidence": [], "levels": {"close": 12.0}}}
    main.store.save_plan({"symbol": "AIOK", "side": "long", "entry": 14, "stop": 12.5, "targets": [{"price": 16}]})
    assert main.alerts_from(data) == 2  # the recent breakout and the plan stop; the old signal is ignored
    assert main.alerts_from(data) == 0  # no duplicates on the next check
    titles = [a["title"] for a in main.store.alerts()]
    assert any("Breakout confirmed" in t for t in titles) and any("stop level was reached" in t for t in titles)


def test_adjusted_bars_are_cached_separately():
    assert main.cache_key("raw") == "iex" and main.cache_key("split") == "iex+split"


def test_daily_chart_uses_30_minute_bars(monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: ny(2026, 10, 27, 12, 0))
    main.store = Store(":memory:")
    data = TestClient(main.app).get("/api/candles", params={"symbol": "AIOK", "timeframe": "1Day"}).json()
    assert data["provenance"]["source"].startswith("30-minute")

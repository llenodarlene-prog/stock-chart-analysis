import math

import httpx

from app import scanner
from app.alpaca import AlpacaClient, Bar
from app.config import AnalysisConfig, Settings
from app.store import Store
from tests.helpers import ny

SETTINGS = Settings("k", "s", "iex", "https://data.example", "https://trade.example", ":memory:")


def thirty_min_bars(days, start_price=10.0, drift=0.002, wave=0.0):
    """Regular-session 30-minute fixture bars for the scanner tests only."""
    bars, price, i = [], start_price, 0
    from datetime import date, timedelta
    d = date(2026, 4, 1)
    made = 0
    while made < days:
        if d.weekday() < 5:
            for k in range(13):
                i += 1
                o = price
                price *= 1 + drift / 13 + wave * math.sin(i / 9) / 13
                t = ny(d.year, d.month, d.day, 9, 30) + k * 1800
                bars.append(Bar(t, o, max(o, price) * 1.001, min(o, price) * 0.999, price, 5000, 10, None))
            made += 1
        d += timedelta(days=1)
    return bars


def test_directory_parser_uses_link_path_for_ticker():
    html = '<a href="/en/us-stock/aiok/"><div>AI OKTO CORP. Common Stock (AIOK)</div></a><a href="/en/us-stock/rlx/">RLX ADS (each representing one (1) share) (RLX)</a><a href="/en/us-stock/brk.b/">Berkshire Hathaway (BRK.B)</a>'
    rows = dict(scanner.parse_directory(html))
    assert rows == {"AIOK": "AI OKTO CORP. Common Stock", "RLX": "RLX ADS (each representing one (1) share)", "BRK.B": "Berkshire Hathaway"}


def test_universe_refresh_records_source_date_and_rejects_bad_pages():
    store = Store(":memory:")
    page = "".join(f'<a href="/en/us-stock/s{i}/">Stock {i} (S{i})</a>' for i in range(150))
    ok = httpx.MockTransport(lambda r: httpx.Response(200, text=page, headers={"last-modified": "Thu, 29 Oct 2026 00:57:41 GMT"}))
    info = scanner.refresh_universe(store, transport=ok)
    assert info["symbols"] == 150 and info["source_last_modified"].startswith("Thu, 29 Oct 2026")
    bad = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>maintenance</html>"))
    try:
        scanner.refresh_universe(store, transport=bad)
        assert False, "expected failure"
    except Exception as exc:
        assert "previous universe was kept" in str(exc)
    assert len(store.universe()) == 150


def test_setups_never_use_future_candles():
    acfg, cfg = AnalysisConfig(), scanner.ScanConfig()
    candles = [c for c in scanner.daily_candles(thirty_min_bars(140, wave=0.03), ny(2026, 12, 1, 0, 0)) if c.complete]
    for t in range(cfg.min_history_days, len(candles) - 1, 7):
        full = scanner.evaluate(candles, acfg, cfg, t=t)
        cut = scanner.evaluate(candles[: t + 1], acfg, cfg, t=t)
        assert [(s["setup"], s["date"]) for s in full] == [(s["setup"], s["date"]) for s in cut]


def test_high_rsi_momentum_is_not_rejected_for_rsi_alone():
    acfg, cfg = AnalysisConfig(), scanner.ScanConfig(momentum_min_rvol=0.0)
    candles = [c for c in scanner.daily_candles(thirty_min_bars(140, drift=0.01, wave=0.08), ny(2026, 12, 1, 0, 0)) if c.complete]
    from app.indicators import compute_all
    from app.structure import swings
    ind = compute_all(candles, acfg, daily=True)
    sw = swings(candles, acfg)
    hits = [t for t in range(cfg.min_history_days, len(candles)) if any(s["setup"] == "momentum_continuation" for s in scanner.evaluate(candles, acfg, cfg, t=t, ind=ind, all_swings=sw))]
    assert hits, "an uptrend with pullbacks should produce continuation setups"
    t = hits[-1]
    hot = {k: list(v) for k, v in ind.items()}
    hot["rsi"][t] = 92.0  # an extreme reading changes nothing by itself
    assert any(s["setup"] == "momentum_continuation" for s in scanner.evaluate(candles, acfg, cfg, t=t, ind=hot, all_swings=sw))


def test_select_caps_per_setup_and_returns_fewer_when_few_qualify():
    def cand(setup, met):
        return {"setup": setup, "optional": {"a": met, "b": False}, "catalyst": {"status": "none found"}, "liquidity": {"iex_avg_dollar_volume": 1}}
    many = [cand("momentum_continuation", True) for _ in range(9)] + [cand("developing_breakout", False) for _ in range(3)]
    picked = scanner.select(many, scanner.ScanConfig())
    assert len(picked) == 7 and sum(1 for c in picked if c["setup"] == "momentum_continuation") == 4
    assert "Not a probability" in picked[0]["rank_note"]
    assert scanner.select([cand("uptrend_pullback", True)], scanner.ScanConfig())[0]["rank"] == 1


def test_overnight_and_swing_backtests_are_reported_separately():
    store = Store(":memory:")
    store.upsert_bars_30m("TEST", "iex", thirty_min_bars(150, drift=0.01, wave=0.08))
    out = scanner.backtest(store, "iex", ["TEST"], AnalysisConfig(), scanner.ScanConfig(momentum_min_rvol=0.0, lookback_days=400), ny(2026, 12, 1, 0, 0))
    assert set(out["results"]) == {"overnight", "swing"}
    for horizon in out["results"].values():
        assert set(horizon) == {"developing_breakout", "momentum_continuation", "uptrend_pullback"}
    assert any(v["in_sample"]["trades"] for h in out["results"].values() for v in h.values())


def test_reassess_flags_gap_through_invalidation_and_new_news():
    scan = {"created_at": ny(2026, 10, 28, 16, 0), "candidates": [{"symbol": "AIOK", "close": 14.0, "atr": 0.5, "catalyst": {"articles": []}, "plans": {"overnight": {"horizon": "overnight", "stop": 13.6, "trigger": 14.0}, "swing": {"horizon": "swing", "stop": 13.0, "trigger": 14.3}}}]}

    def handler(request):
        if request.url.path.endswith("/snapshots"):
            return httpx.Response(200, json={"AIOK": {"latestTrade": {"p": 13.4, "t": "2026-10-29T12:00:00Z"}}})
        return httpx.Response(200, json={"news": [{"id": 1, "headline": "Offering priced", "symbols": ["AIOK"], "created_at": "2026-10-29T11:00:00Z", "url": "https://example.com/a", "source": "test"}], "next_page_token": None})

    client = AlpacaClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    out = scanner.reassess(scan, client, ny(2026, 10, 29, 8, 0))
    r = out["candidates"][0]["reassessment"]
    assert r["status"] == "invalidated by gap"
    assert any("new article" in n for n in r["notes"])

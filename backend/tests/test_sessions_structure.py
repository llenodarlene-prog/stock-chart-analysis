from app.config import AnalysisConfig
from app.indicators import compute_all
from app.sessions import aggregate, data_hash
from app.structure import detect_signals, swings
from tests.helpers import minute_bars, ny


def test_regular_session_excludes_premarket_and_anchors_hourly_at_open():
    bars = minute_bars(ny(2026, 10, 26, 9, 0), [10 + i * 0.01 for i in range(120)])  # 09:00-10:59
    regular = aggregate(bars, "1Hour", {"regular"}, now=ny(2026, 10, 27, 0, 0))
    assert [c.session for c in regular] == ["regular", "regular"]
    assert regular[0].time == ny(2026, 10, 26, 9, 30) and regular[0].end == ny(2026, 10, 26, 10, 30)
    assert regular[0].minutes == 60 and regular[1].minutes == 30
    extended = aggregate(bars, "1Hour", {"pre", "regular", "post"}, now=ny(2026, 10, 27, 0, 0))
    assert extended[0].session == "pre" and extended[0].time == ny(2026, 10, 26, 9, 0) and extended[0].end == ny(2026, 10, 26, 9, 30)


def test_forming_candle_is_marked_incomplete_until_bucket_end_plus_grace():
    bars = minute_bars(ny(2026, 10, 26, 9, 30), [10.0] * 7)  # 09:30-09:36
    now = ny(2026, 10, 26, 9, 37)
    five = aggregate(bars, "5Min", {"regular"}, now)
    assert [c.complete for c in five] == [True, False]
    assert five[0].complete  # 09:35 end + 60 s grace <= 09:37
    assert not aggregate(bars, "5Min", {"regular"}, ny(2026, 10, 26, 9, 35))[0].complete


def test_daily_candle_complete_only_after_session_close():
    bars = minute_bars(ny(2026, 10, 26, 9, 30), [10.0, 11.0, 9.0])
    assert not aggregate(bars, "1Day", {"regular"}, ny(2026, 10, 26, 15, 0))[0].complete
    assert aggregate(bars, "1Day", {"regular"}, ny(2026, 10, 26, 16, 2))[0].complete


def test_hash_changes_with_any_candle_value():
    bars = minute_bars(ny(2026, 10, 26, 9, 30), [10.0, 10.5])
    a = aggregate(bars, "1Min", {"regular"}, ny(2026, 10, 26, 12, 0))
    h = data_hash(a)
    a[1].close += 0.000001
    assert data_hash(a) != h


def zigzag():
    # Rises to a peak at index 10, dips, then breaks out above it.
    closes = [10 + 0.1 * i for i in range(11)] + [11 - 0.1 * i for i in range(1, 6)] + [10.5 + 0.2 * i for i in range(1, 12)]
    bars = minute_bars(ny(2026, 10, 26, 9, 30), closes)
    bars[10].h += 0.05  # a clear peak; neighbouring bars share an open/close price
    return bars


def test_swing_is_not_available_before_its_confirmation_bars():
    cfg = AnalysisConfig(swing_left=3, swing_right=3)
    candles = aggregate(zigzag(), "1Min", {"regular"}, ny(2026, 10, 27, 0, 0))
    found = [s for s in swings(candles, cfg) if s.kind == "high"]
    assert found and found[0].index == 10 and found[0].confirmed_index == 13
    assert not [s for s in swings(candles, cfg, upto=12) if s.kind == "high"]
    assert [s for s in swings(candles, cfg, upto=13) if s.kind == "high"]


def test_signals_have_no_look_ahead():
    cfg = AnalysisConfig(swing_left=3, swing_right=3, rvol_min_samples=1, breakout_min_rvol=0.0, atr_period=3)
    candles = aggregate(zigzag(), "1Min", {"regular"}, ny(2026, 10, 27, 0, 0))
    full = detect_signals(candles, compute_all(candles, cfg, False), cfg)
    for cut in range(5, len(candles)):
        part = candles[:cut]
        prefix = detect_signals(part, compute_all(part, cfg, False), cfg)
        assert [(s["kind"], s["variant"], s["time"]) for s in prefix] == [(s["kind"], s["variant"], s["time"]) for s in full if s["index"] < cut]
    assert any(s["kind"] == "breakout" and s["direction"] == "bullish" for s in full)


def test_forming_candle_never_produces_a_signal():
    cfg = AnalysisConfig(swing_left=3, swing_right=3, rvol_min_samples=1, breakout_min_rvol=0.0, atr_period=3)
    bars = zigzag()
    last_ts = bars[-1].t
    candles = aggregate(bars, "1Min", {"regular"}, now=last_ts + 30)  # last minute still forming
    assert not candles[-1].complete
    signals = detect_signals(candles, compute_all(candles, cfg, False), cfg)
    assert all(s["index"] < len(candles) - 1 for s in signals)


def test_calendar_early_close_and_holiday():
    from app import sessions
    sessions.load_calendar([{"date": "2026-11-27", "open": "09:30", "close": "13:00", "session_open": "0400", "session_close": "1700"}, {"date": "2026-11-25", "open": "09:30", "close": "16:00", "session_open": "0400", "session_close": "2000"}])
    try:
        assert sessions.market_state(ny(2026, 11, 26, 11, 0)) == "closed"  # Thanksgiving, inside the calendar range
        assert sessions.session_of(ny(2026, 11, 27, 13, 30))[0] == "post"  # after the early close
        bars = minute_bars(ny(2026, 11, 27, 12, 30), [10.0] * 30)  # 12:30-12:59
        hourly = aggregate(bars, "1Hour", {"regular"}, ny(2026, 11, 28, 0, 0))
        assert hourly[-1].end == ny(2026, 11, 27, 13, 0)
        assert sessions.expected_buckets(hourly, "1Hour", {"regular"}) == 4  # 09:30-13:00 in hourly buckets
    finally:
        sessions.load_calendar([])

import math

from app.config import AnalysisConfig
from app.indicators import atr, ema, macd, relative_volume, rsi, session_vwap
from app.sessions import Candle


def candle(i, o, h, l, c, v=100, session="regular", day="2026-10-26", slot=None):
    return Candle(i * 60, i * 60 + 60, o, h, l, c, v, session, day, i if slot is None else slot, 1, True)


def test_ema_seed_and_recursion():
    values = [1, 2, 3, 4, 5, 6]
    out = ema(values, 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0  # simple average of 1, 2, 3
    assert math.isclose(out[3], 2 + (4 - 2) * 0.5)
    assert math.isclose(out[5], 5.0)


def naive_rsi(closes, n):
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    g, l = sum(gains[:n]) / n, sum(losses[:n]) / n
    out = {n: 100 - 100 / (1 + g / l)}
    for i in range(n, len(gains)):
        g = (g * (n - 1) + gains[i]) / n
        l = (l * (n - 1) + losses[i]) / n
        out[i + 1] = 100 - 100 / (1 + g / l)
    return out


def test_rsi_matches_independent_wilder_calculation():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    out = rsi(closes, 14)
    assert all(v is None for v in out[:14])
    for i, value in naive_rsi(closes, 14).items():
        assert math.isclose(out[i], value, rel_tol=1e-12)
    assert 70 < out[14] < 71  # widely published value for this series is about 70.5


def test_rsi_edge_cases():
    assert rsi([1, 2, 3, 4], 3)[3] == 100.0
    assert rsi([5, 5, 5, 5], 3)[3] == 50.0


def test_macd_is_difference_of_emas_and_signal_waits_for_history():
    closes = [float(i % 7 + i * 0.3) for i in range(60)]
    m = macd(closes, 12, 26, 9)
    f, s = ema(closes, 12), ema(closes, 26)
    assert m["macd"][24] is None and m["macd"][25] is not None
    assert math.isclose(m["macd"][40], f[40] - s[40])
    assert m["signal"][32] is None and m["signal"][33] is not None
    assert math.isclose(m["histogram"][50], m["macd"][50] - m["signal"][50])


def test_atr_wilder():
    cs = [candle(0, 10, 11, 9, 10), candle(1, 10, 12, 10, 11), candle(2, 11, 11.5, 8, 9), candle(3, 9, 10, 9, 9.5)]
    out = atr(cs, 2)
    # true ranges: 2, 2, 3.5 (11 - 8 vs prev close 11), 1
    assert out[0] is None and out[1] == 2.0
    assert math.isclose(out[2], (2.0 * 1 + 3.5) / 2)
    assert math.isclose(out[3], (out[2] * 1 + 1.0) / 2)


def test_vwap_resets_each_session_segment():
    cs = [candle(0, 10, 10, 10, 10, 100, day="d1"), candle(1, 20, 20, 20, 20, 100, day="d1"), candle(2, 30, 30, 30, 30, 50, day="d2")]
    out = session_vwap(cs, daily=False)
    assert out == [10.0, 15.0, 30.0]
    assert session_vwap(cs, daily=True) == [None, None, None]


def test_relative_volume_uses_only_prior_sessions_same_slot():
    cs = []
    for d in range(7):
        cs.append(candle(d * 10, 1, 1, 1, 1, 100 * (d + 1), day=f"2026-10-{10 + d}", slot=0))
    out = relative_volume(cs, daily=False, sessions=10, min_samples=5, daily_lookback=20)
    assert out[:5] == [None] * 5  # fewer than 5 prior sessions
    assert math.isclose(out[5], 600 / ((100 + 200 + 300 + 400 + 500) / 5))

"""Indicator calculations. Each returns one value per candle, with None where
history is insufficient. Formulas:

- EMA(n): seeded with the simple average of the first n closes, then
  ema = prev + (close - prev) * 2 / (n + 1).
- RSI(n): Wilder smoothing of gains and losses, seeded with simple averages
  of the first n changes.
- MACD(f, s, sig): EMA(f) - EMA(s); signal = EMA(sig) of MACD; histogram = MACD - signal.
- ATR(n): Wilder smoothing of true range, seeded with the simple average of
  the first n true ranges. The first candle's true range is high - low.
- Session VWAP: cumulative (typical price x volume) / cumulative volume,
  reset at the start of each session segment. Typical price = (H + L + C) / 3.
- Relative volume (intraday): volume / mean volume of the same slot in the
  previous N sessions of the same type. Daily: volume / mean of prior N days.
"""
from __future__ import annotations

from .sessions import Candle


def ema(values: list[float | None], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < period:
        return out
    seed_end = start + period
    prev = sum(values[start:seed_end]) / period  # type: ignore[arg-type]
    out[seed_end - 1] = prev
    k = 2 / (period + 1)
    for i in range(seed_end, len(values)):
        prev = prev + (values[i] - prev) * k  # type: ignore[operator]
        out[i] = prev
    return out


def rsi(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def macd(closes: list[float], fast: int, slow: int, signal: int) -> dict[str, list[float | None]]:
    f, s = ema(closes, fast), ema(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    sig = ema(line, signal)
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return {"macd": line, "signal": sig, "histogram": hist}


def true_range(candles: list[Candle]) -> list[float]:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            prev = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    return out


def atr(candles: list[Candle], period: int) -> list[float | None]:
    tr = true_range(candles)
    out: list[float | None] = [None] * len(candles)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def session_vwap(candles: list[Candle], daily: bool) -> list[float | None]:
    if daily:
        return [None] * len(candles)
    out: list[float | None] = []
    key = None
    pv = vol = 0.0
    for c in candles:
        segment = (c.session_date, c.session)
        if segment != key:
            key, pv, vol = segment, 0.0, 0.0
        pv += (c.high + c.low + c.close) / 3 * c.volume
        vol += c.volume
        out.append(pv / vol if vol else None)
    return out


def relative_volume(candles: list[Candle], daily: bool, sessions: int, min_samples: int, daily_lookback: int) -> list[float | None]:
    out: list[float | None] = []
    if daily:
        for i, c in enumerate(candles):
            prior = [x.volume for x in candles[max(0, i - daily_lookback):i]]
            out.append(c.volume / (sum(prior) / len(prior)) if len(prior) >= min(daily_lookback, max(min_samples, 1)) and sum(prior) > 0 else None)
        return out
    history: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for c in candles:
        key = (c.session, c.slot)
        past = [v for d, v in history.get(key, []) if d < c.session_date][-sessions:]
        mean = sum(past) / len(past) if past else 0
        out.append(c.volume / mean if len(past) >= min_samples and mean > 0 else None)
        history.setdefault(key, []).append((c.session_date, c.volume))
    return out


def compute_all(candles: list[Candle], cfg, daily: bool) -> dict[str, list[float | None]]:
    closes = [c.close for c in candles]
    m = macd(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    return {
        "ema_fast": ema(closes, cfg.ema_fast),
        "ema_slow": ema(closes, cfg.ema_slow),
        "rsi": rsi(closes, cfg.rsi_period),
        "macd": m["macd"],
        "macd_signal": m["signal"],
        "macd_hist": m["histogram"],
        "atr": atr(candles, cfg.atr_period),
        "vwap": session_vwap(candles, daily),
        "rvol": relative_volume(candles, daily, cfg.rvol_sessions, cfg.rvol_min_samples, cfg.rvol_daily_lookback),
    }

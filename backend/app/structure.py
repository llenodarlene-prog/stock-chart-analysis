"""Swing points, trend, and breakout signals.

Every function here reads candles up to an index and nothing after it, so a
value computed at bar i is the value a live system would have had at bar i's
close. Swing points carry `confirmed_index`: the bar whose close confirms
them. Callers must ignore swings whose confirmed_index is later than the bar
being evaluated.

Rules (all thresholds come from AnalysisConfig):

Swing high at bar i: high[i] > every high in the `swing_left` bars before it
and > every high in the `swing_right` bars after it. Confirmed at i + swing_right.
Swing low: the mirror image using lows.

Trend at bar t, from the two most recent swings of each type confirmed by t:
up when the last swing high and last swing low are both higher than the ones
before; down when both are lower; range otherwise; insufficient with fewer
than two of each.

Breakout (bullish) at completed bar t, against resistance R = the most recent
swing high confirmed by bar t-1:
- early: close[t] > R and close[t-1] <= R. No buffer or volume filter.
- confirmed: close[t] > R + buffer and close[t-1] <= R + buffer, where
  buffer = breakout_buffer_atr x ATR[t-1], and relative volume >= breakout_min_rvol.
- retest: after a confirmed breakout at bar b, the first bar t in
  (b, b + retest_window] with low[t] <= R + retest_tolerance_atr x ATR[b]
  and close[t] > R.
Failed breakout: after a confirmed breakout at b, a bar t in
(b, b + failed_breakout_window] that closes below R - buffer[b].
Bearish breakdowns mirror these rules against the latest swing low.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import AnalysisConfig
from .sessions import Candle


@dataclass
class Swing:
    kind: str  # high or low
    index: int
    confirmed_index: int
    price: float
    time: int
    confirmed_time: int


def swings(candles: list[Candle], cfg: AnalysisConfig, upto: int | None = None) -> list[Swing]:
    """Swings confirmed by bar `upto` (inclusive), using completed candles only."""
    last = len(candles) - 1 if upto is None else upto
    out: list[Swing] = []
    left, right = cfg.swing_left, cfg.swing_right
    for i in range(left, last - right + 1):
        if not all(candles[j].complete for j in range(i - left, i + right + 1)):
            continue
        h, l = candles[i].high, candles[i].low
        if all(h > candles[j].high for j in range(i - left, i)) and all(h > candles[j].high for j in range(i + 1, i + right + 1)):
            out.append(Swing("high", i, i + right, h, candles[i].time, candles[i + right].time))
        if all(l < candles[j].low for j in range(i - left, i)) and all(l < candles[j].low for j in range(i + 1, i + right + 1)):
            out.append(Swing("low", i, i + right, l, candles[i].time, candles[i + right].time))
    return out


def confirmed_by(all_swings: list[Swing], t: int, kind: str) -> list[Swing]:
    return [s for s in all_swings if s.kind == kind and s.confirmed_index <= t]


def trend_at(all_swings: list[Swing], t: int) -> dict:
    highs, lows = confirmed_by(all_swings, t, "high"), confirmed_by(all_swings, t, "low")
    if len(highs) < 2 or len(lows) < 2:
        return {"direction": "insufficient", "reason": f"Needs two confirmed swing highs and two swing lows; has {len(highs)} and {len(lows)}."}
    hh, hl = highs[-1].price > highs[-2].price, lows[-1].price > lows[-2].price
    lh, ll = highs[-1].price < highs[-2].price, lows[-1].price < lows[-2].price
    detail = f"Swing highs {highs[-2].price:.4g} then {highs[-1].price:.4g}; swing lows {lows[-2].price:.4g} then {lows[-1].price:.4g}."
    if hh and hl:
        return {"direction": "up", "reason": f"Higher high and higher low. {detail}"}
    if lh and ll:
        return {"direction": "down", "reason": f"Lower high and lower low. {detail}"}
    return {"direction": "range", "reason": f"Swing structure is mixed. {detail}"}


def detect_signals(candles: list[Candle], ind: dict, cfg: AnalysisConfig) -> list[dict]:
    """All breakout, retest, and failed-breakout events on completed candles."""
    all_swings = swings(candles, cfg)
    atr, rvol = ind["atr"], ind["rvol"]
    events: list[dict] = []
    active: list[dict] = []  # confirmed breakouts awaiting retest / failure
    for t in range(1, len(candles)):
        c, prev = candles[t], candles[t - 1]
        if not c.complete:
            break
        for direction, kind in (("bullish", "high"), ("bearish", "low")):
            levels = confirmed_by(all_swings, t - 1, kind)
            if not levels or atr[t - 1] is None:
                continue
            level = levels[-1].price
            sign = 1 if direction == "bullish" else -1
            buffer = cfg.breakout_buffer_atr * atr[t - 1]
            beyond = lambda price, extra=0.0: sign * (price - level) > extra
            base = {"direction": direction, "time": c.time, "index": t, "level": level, "swing_time": levels[-1].time}
            if beyond(c.close) and not beyond(prev.close):
                events.append({**base, "kind": "breakout", "variant": "early", "rule": "Close crossed the latest confirmed swing level with no buffer or volume filter.", "confirmation": "weak", "evidence": {"close": c.close, "previous_close": prev.close, "level": level}})
            if beyond(c.close, buffer) and not beyond(prev.close, buffer):
                volume_ok = rvol[t] is not None and rvol[t] >= cfg.breakout_min_rvol
                evidence = {"close": c.close, "level": level, "buffer": buffer, "atr": atr[t - 1], "rvol": rvol[t], "min_rvol": cfg.breakout_min_rvol}
                if volume_ok:
                    event = {**base, "kind": "breakout", "variant": "confirmed", "rule": f"Close beyond level by {cfg.breakout_buffer_atr} ATR with relative volume >= {cfg.breakout_min_rvol}.", "confirmation": "candle", "evidence": evidence, "buffer": buffer, "atr_at": atr[t - 1]}
                    events.append(event)
                    active.append({**event, "retested": False, "failed": False})
                else:
                    events.append({**base, "kind": "breakout_unconfirmed_volume", "variant": "confirmed", "rule": "Price cleared the buffer but relative volume was below the minimum or unavailable.", "confirmation": "none", "evidence": evidence})
        for b in active:
            age = t - b["index"]
            if age <= 0 or b["failed"]:
                continue
            sign = 1 if b["direction"] == "bullish" else -1
            if not b["retested"] and age <= cfg.retest_window:
                tolerance = cfg.retest_tolerance_atr * b["atr_at"]
                touched = (c.low <= b["level"] + tolerance) if sign == 1 else (c.high >= b["level"] - tolerance)
                held = sign * (c.close - b["level"]) > 0
                if touched and held:
                    b["retested"] = True
                    events.append({"direction": b["direction"], "time": c.time, "index": t, "level": b["level"], "kind": "retest", "variant": "retest", "rule": f"Within {cfg.retest_window} bars of a confirmed breakout, price returned within {cfg.retest_tolerance_atr} ATR of the level and closed on the breakout side.", "confirmation": "retest", "evidence": {"low" if sign == 1 else "high": c.low if sign == 1 else c.high, "close": c.close, "level": b["level"], "breakout_time": b["time"]}})
            if age <= cfg.failed_breakout_window and sign * (c.close - (b["level"] - sign * b["buffer"])) < 0:
                b["failed"] = True
                events.append({"direction": "bearish" if sign == 1 else "bullish", "time": c.time, "index": t, "level": b["level"], "kind": "failed_breakout", "variant": "failed", "rule": f"Within {cfg.failed_breakout_window} bars of a confirmed {b['direction']} breakout, a candle closed back through the level by more than the buffer.", "confirmation": "candle", "evidence": {"close": c.close, "level": b["level"], "breakout_time": b["time"]}})
        active = [b for b in active if t - b["index"] < max(cfg.retest_window, cfg.failed_breakout_window) and not b["failed"]]
    return events

"""Data-quality gates, evidence-based assessment, levels, and scenarios.

The assessment label is a fixed rule over recorded evidence, not a score:
- bullish: trend is up, the last completed close is above the slow EMA, and
  the close has not broken below the latest confirmed swing low.
- bearish: the mirror image.
- neutral: anything else, including insufficient history.
Momentum readings (RSI extremes, MACD turning) are listed as warnings and never
change the label. Candle shape (wicks) is not used as evidence. A profitable
position or a large prior rise is not an input at all.
"""
from __future__ import annotations

from .config import AnalysisConfig
from .sessions import TIMEFRAMES, Candle
from .structure import confirmed_by, swings, trend_at


def quality_gates(candles: list[Candle], ind: dict, cfg: AnalysisConfig, timeframe: str, fetch_age: float | None, market_open: bool, fetch_error: str | None) -> dict:
    blocked: list[str] = []
    notes: list[str] = []
    completed = [c for c in candles if c.complete]
    if fetch_error:
        blocked.append(f"Latest data request failed: {fetch_error}")
    if not candles:
        blocked.append("No candles are available for this symbol, feed, and range.")
    if len(completed) < cfg.min_bars_for_signals:
        blocked.append(f"Insufficient history: {len(completed)} completed candles, {cfg.min_bars_for_signals} required.")
    if market_open and (fetch_age is None or fetch_age > cfg.stale_fetch_seconds):
        blocked.append(f"Data is stale: last successful fetch was {'never' if fetch_age is None else f'{int(fetch_age)} seconds ago'} while a session is open (limit {cfg.stale_fetch_seconds} seconds).")
    needed = max(cfg.ema_slow, cfg.macd_slow + cfg.macd_signal - 1, cfg.atr_period, cfg.rsi_period + 1)
    if completed and ind["atr"][len(completed) - 1] is None:
        blocked.append(f"Indicators need at least {needed} completed candles.")
    # Missing buckets among the last 20 completed candles (no IEX trades in a bucket leaves no candle).
    size = TIMEFRAMES[timeframe]
    recent = completed[-20:]
    if size and len(recent) > 1:
        missing = 0
        for a, b in zip(recent, recent[1:]):
            if (a.session_date, a.session) == (b.session_date, b.session):
                missing += max(0, round((b.time - a.time) / 60 / size) - 1)
        ratio = missing / (missing + len(recent))
        if missing:
            notes.append(f"{missing} empty {timeframe} buckets among the last {len(recent)} completed candles (no trades on this feed).")
        if ratio > cfg.max_recent_gap_ratio:
            blocked.append(f"Too many missing recent candles: {ratio:.0%} of recent buckets are empty (limit {cfg.max_recent_gap_ratio:.0%}).")
    if candles and not candles[-1].complete:
        notes.append("The last candle is still forming. It is drawn separately and excluded from signals.")
    return {"actionable": not blocked, "blocked_reasons": blocked, "notes": notes}


def _item(category: str, direction: str, text: str, value=None) -> dict:
    return {"category": category, "direction": direction, "text": text, "value": value}


def assess(candles: list[Candle], ind: dict, cfg: AnalysisConfig, signals: list[dict], gates: dict, timeframe: str = "") -> dict:
    completed = [c for c in candles if c.complete]
    if len(completed) < 2:
        return {"label": "neutral", "reason": "Insufficient history for an assessment.", "evidence": [], "levels": {}, "scenarios": [], "as_of": None}
    t = len(completed) - 1
    c = completed[t]
    all_swings = swings(completed, cfg)
    trend = trend_at(all_swings, t)
    highs, lows = confirmed_by(all_swings, t, "high"), confirmed_by(all_swings, t, "low")
    resistance = highs[-1].price if highs else None
    support = lows[-1].price if lows else None
    v = lambda key: ind[key][t]
    evidence: list[dict] = []
    trend_dir = {"up": "bullish", "down": "bearish"}.get(trend["direction"], "neutral")
    evidence.append(_item("trend_continuation", trend_dir, f"Swing structure: {trend['direction']}. {trend['reason']}"))
    if v("ema_slow") is not None:
        evidence.append(_item("trend_continuation", "bullish" if c.close > v("ema_slow") else "bearish", f"Close {c.close:.4f} is {'above' if c.close > v('ema_slow') else 'below'} EMA {cfg.ema_slow} ({v('ema_slow'):.4f}).", v("ema_slow")))
    if v("ema_fast") is not None and v("ema_slow") is not None:
        evidence.append(_item("trend_continuation", "bullish" if v("ema_fast") > v("ema_slow") else "bearish", f"EMA {cfg.ema_fast} is {'above' if v('ema_fast') > v('ema_slow') else 'below'} EMA {cfg.ema_slow}."))
    if v("vwap") is not None:
        evidence.append(_item("trend_continuation", "bullish" if c.close > v("vwap") else "bearish", f"Close is {'above' if c.close > v('vwap') else 'below'} session VWAP ({v('vwap'):.4f}).", v("vwap")))
    if v("macd_hist") is not None:
        evidence.append(_item("momentum", "bullish" if v("macd_hist") > 0 else "bearish", f"MACD histogram is {v('macd_hist'):+.4f}.", v("macd_hist")))
        prev_hist = ind["macd_hist"][t - 1]
        if prev_hist is not None and abs(v("macd_hist")) < abs(prev_hist) and (v("macd_hist") > 0) == (prev_hist > 0):
            evidence.append(_item("momentum_warning", "neutral", "MACD histogram is shrinking toward zero. This is a momentum warning, not an invalidation."))
    if v("rsi") is not None:
        r = v("rsi")
        evidence.append(_item("momentum", "bullish" if r > 50 else "bearish" if r < 50 else "neutral", f"RSI {cfg.rsi_period} is {r:.1f}.", r))
        if r >= cfg.rsi_overbought:
            evidence.append(_item("momentum_warning", "neutral", f"RSI is at or above {cfg.rsi_overbought:g}. Strong trends often stay overbought, so this is a warning only and does not by itself justify exiting."))
        if r <= cfg.rsi_oversold:
            evidence.append(_item("momentum_warning", "neutral", f"RSI is at or below {cfg.rsi_oversold:g}. This is a warning only and does not by itself justify entering."))
    if v("rvol") is not None:
        evidence.append(_item("volume", "neutral", f"Relative volume is {v('rvol'):.2f}x the same period's recent average.", v("rvol")))
    else:
        evidence.append(_item("volume", "neutral", "Relative volume: insufficient comparable history."))
    recent = [s for s in signals if s["index"] >= t - 10 and s["kind"] in ("breakout", "retest", "failed_breakout") and s.get("variant") != "early"]
    for s in recent[-3:]:
        evidence.append(_item("entry_qualification", s["direction"], f"{s['kind'].replace('_', ' ').capitalize()} at level {s['level']:.4f} ({s['variant']})."))
    invalid_bull = support is not None and c.close < support
    invalid_bear = resistance is not None and c.close > resistance
    if invalid_bull:
        evidence.append(_item("invalidation", "bearish", f"Close is below the latest confirmed swing low {support:.4f}. Bullish structure is invalidated."))
    if invalid_bear:
        evidence.append(_item("invalidation", "bullish", f"Close is above the latest confirmed swing high {resistance:.4f}. Bearish structure is invalidated."))
    above_slow = v("ema_slow") is not None and c.close > v("ema_slow")
    below_slow = v("ema_slow") is not None and c.close < v("ema_slow")
    if trend["direction"] == "up" and above_slow and not invalid_bull:
        label, reason = "bullish", "Trend is up, close is above the slow EMA, and the latest swing low holds."
    elif trend["direction"] == "down" and below_slow and not invalid_bear:
        label, reason = "bearish", "Trend is down, close is below the slow EMA, and the latest swing high holds."
    else:
        label, reason = "neutral", "The trend, EMA, and invalidation conditions do not all agree."
    atr = v("atr")
    buffer = cfg.breakout_buffer_atr * atr if atr else 0.0
    higher = sorted({s.price for s in highs if s.price > c.close})
    lower = sorted({s.price for s in lows if s.price < c.close}, reverse=True)
    levels = {
        "resistance": resistance, "support": support, "buffer": buffer, "atr": atr, "close": c.close,
        "swing_highs_above": higher[:3], "swing_lows_below": lower[:3],
        "measured_move_up": resistance + (resistance - support) if resistance is not None and support is not None and resistance > support else None,
        "measured_move_down": support - (resistance - support) if resistance is not None and support is not None and resistance > support else None,
    }
    supporting = [e for e in evidence if e["direction"] == label] if label != "neutral" else [e for e in evidence if e["direction"] == "bullish"]
    opposing = [e for e in evidence if e["direction"] not in (label, "neutral")] if label != "neutral" else [e for e in evidence if e["direction"] == "bearish"]
    return {
        "label": label, "reason": reason, "as_of": c.time, "timeframe_close": c.close,
        "trend": trend, "evidence": evidence, "supporting": supporting, "opposing": opposing,
        "warnings": [e for e in evidence if e["category"] == "momentum_warning"],
        "levels": levels, "scenarios": scenarios(levels, evidence, cfg, gates),
        "next_steps": next_steps(label, trend["direction"], levels, recent, evidence, gates, timeframe),
        "actionable": gates["actionable"], "blocked_reasons": gates["blocked_reasons"],
        "disclaimer": "Scenarios are conditional on the stated triggers. They are not forecasts and carry no probability. Gaps and slippage can cause losses beyond a planned stop.",
    }


def scenarios(levels: dict, evidence: list[dict], cfg: AnalysisConfig, gates: dict) -> list[dict]:
    r, s, buf = levels["resistance"], levels["support"], levels["buffer"]
    fmt = lambda x: f"{x:.4f}" if x is not None else "not available"
    support_for = lambda d: [e["text"] for e in evidence if e["direction"] == d]
    out = []
    bull_targets = [{"price": p, "source": "prior confirmed swing high"} for p in levels["swing_highs_above"] if r is None or p > r]
    if levels["measured_move_up"]:
        bull_targets.append({"price": levels["measured_move_up"], "source": "measured move: resistance + (resistance - support)"})
    out.append({
        "name": "bullish", "trigger": f"A completed candle closes above {fmt(r + buf if r is not None else None)} (resistance {fmt(r)} + {cfg.breakout_buffer_atr} ATR) with relative volume >= {cfg.breakout_min_rvol}.",
        "targets": bull_targets or [{"price": None, "source": "No confirmed structure above. No target is projected."}],
        "invalidation": f"A completed candle closes back below {fmt(r - buf if r is not None else None)} within {cfg.failed_breakout_window} candles (failed breakout), or below support {fmt(s)}.",
        "supporting_now": support_for("bullish"), "opposing_now": support_for("bearish"),
    })
    out.append({
        "name": "sideways", "trigger": f"Completed candles keep closing between support {fmt(s)} and resistance {fmt(r)}.",
        "targets": [{"price": s, "source": "range floor"}, {"price": r, "source": "range ceiling"}],
        "invalidation": "A confirmed breakout or breakdown candle beyond either level.",
        "supporting_now": [e["text"] for e in evidence if e["direction"] == "neutral" and e["category"] != "volume"], "opposing_now": [],
    })
    bear_targets = [{"price": p, "source": "prior confirmed swing low"} for p in levels["swing_lows_below"] if s is None or p < s]
    if levels["measured_move_down"]:
        bear_targets.append({"price": levels["measured_move_down"], "source": "measured move: support - (resistance - support)"})
    out.append({
        "name": "bearish", "trigger": f"A completed candle closes below {fmt(s - buf if s is not None else None)} (support {fmt(s)} - {cfg.breakout_buffer_atr} ATR) with relative volume >= {cfg.breakout_min_rvol}.",
        "targets": bear_targets or [{"price": None, "source": "No confirmed structure below. No target is projected."}],
        "invalidation": f"A completed candle closes back above {fmt(s + buf if s is not None else None)} within {cfg.failed_breakout_window} candles, or above resistance {fmt(r)}.",
        "supporting_now": support_for("bearish"), "opposing_now": support_for("bullish"),
    })
    for item in out:
        item["actionable"] = gates["actionable"]
    return out


def explain(signal: dict) -> str:
    """Plain-language description built only from the signal's recorded fields."""
    ev = signal.get("evidence", {})
    side = "above" if signal["direction"] == "bullish" else "below"
    if signal["kind"] == "breakout" and signal["variant"] == "early":
        return f"Early {signal['direction']} signal: the candle closed at {ev.get('close'):.4f}, {side} the swing level {signal['level']:.4f}, after the prior close of {ev.get('previous_close'):.4f}. No buffer or volume check, so confirmation is weaker."
    if signal["kind"] == "breakout":
        return f"Confirmed {signal['direction']} breakout: close {ev.get('close'):.4f} cleared {signal['level']:.4f} by more than {ev.get('buffer'):.4f} with relative volume {ev.get('rvol'):.2f}x."
    if signal["kind"] == "breakout_unconfirmed_volume":
        rv = ev.get("rvol")
        return f"Price cleared {signal['level']:.4f} by the buffer, but relative volume was {'unavailable' if rv is None else f'{rv:.2f}x'} versus the {ev.get('min_rvol')}x minimum. Not a confirmed breakout."
    if signal["kind"] == "retest":
        return f"Retest held: price returned to the {signal['level']:.4f} level and closed at {ev.get('close'):.4f} on the breakout side."
    if signal["kind"] == "failed_breakout":
        return f"Failed breakout: close {ev.get('close'):.4f} moved back through {signal['level']:.4f} soon after the breakout."
    return signal.get("rule", "")


def next_steps(label: str, trend: str, levels: dict, recent: list[dict], evidence: list[dict], gates: dict, timeframe: str) -> dict:
    """Plain-language steps built only from the levels and rules above.
    Every price here is an algorithm-derived level shown elsewhere in the
    response; nothing new is invented. Steps stay conditional: the user decides."""
    tf = {"1Min": "1-minute", "5Min": "5-minute", "15Min": "15-minute", "1Hour": "hourly", "1Day": "daily"}.get(timeframe, timeframe or "")
    candle = f"{tf} candle" if tf else "candle"
    p = lambda x: f"${x:,.2f}" if x is not None else "n/a"
    r, s, buf, close = levels["resistance"], levels["support"], levels["buffer"] or 0.0, levels["close"]
    up_targets = [t for t in levels["swing_highs_above"] if r is None or t > r][:2]
    if levels["measured_move_up"]:
        up_targets.append(levels["measured_move_up"])
    hot = any(e["category"] == "momentum_warning" and "RSI is at or above" in e["text"] for e in evidence)
    breakout = next((x for x in reversed(recent) if x["kind"] == "breakout" and x["direction"] == "bullish"), None)
    failed = next((x for x in reversed(recent) if x["kind"] == "failed_breakout"), None)
    steps: list[str] = []
    levels_out = {"buy_trigger": None, "stop": s, "targets": up_targets, "warning_level": s - buf if s is not None else None}

    if not gates["actionable"]:
        return {
            "status": "wait", "headline": "Don't act on this chart right now: the data isn't reliable enough yet.",
            "steps": ["Reasons: " + " ".join(gates["blocked_reasons"]), "Refresh in a few minutes, or switch to a longer timeframe (for example daily) for more history."],
            "levels": levels_out, "reminder": "You make the final decision. This app never places trades.",
        }
    if label == "bullish":
        if breakout and close > breakout["level"]:
            headline = f"Uptrend, and price already broke out above {p(breakout['level'])}. Better to wait for a pullback than to chase."
            steps.append(f"Watch for price to dip back near {p(breakout['level'])} and then close above it on a {candle}. That retest is the safer possible buy point.")
            levels_out["buy_trigger"] = breakout["level"]
        else:
            trigger = r + buf if r is not None else None
            headline = "Uptrend. The possible buy point is a breakout, which hasn't happened yet."
            steps.append(f"Watch for a {candle} to close above {p(trigger)}. That is the app's buy signal.")
            levels_out["buy_trigger"] = trigger
    elif label == "bearish":
        headline = "Downtrend. The app shows no buy setup right now."
        steps.append(f"Don't buy yet. The downtrend signal ends only if a {candle} closes above {p(r)}.")
        if s is not None:
            steps.append(f"If price closes below {p(s - buf)}, expect further weakness. If you already own it, that is the app's exit level.")
    else:
        headline = "No clear direction. Best to wait."
        if r is not None:
            steps.append(f"Possible buy only if a {candle} closes above {p(r + buf)}.")
            levels_out["buy_trigger"] = r + buf
        if s is not None:
            steps.append(f"Warning sign if a {candle} closes below {p(s - buf)}.")
    if label != "bearish" and levels_out["buy_trigger"] is not None:
        if s is not None:
            steps.append(f"If you decide to buy, the app's stop (exit if wrong) is {p(s)}. Exit if a {candle} closes below it.")
        if up_targets:
            steps.append("Take-profit areas: " + ", then ".join(p(t) for t in up_targets) + ". These are past turning points and a measured move, not promises.")
        else:
            steps.append("No take-profit area above yet. If it moves your way, raise your stop as it climbs.")
        steps.append("Use the Entry, Exit & Price Projections tab to see how many shares fit your risk.")
    if failed:
        steps.append(f"Caution: a recent breakout failed near {p(failed['level'])}. Breakouts here have not held lately.")
    if hot:
        steps.append("The price has risen fast (RSI is high). Avoid chasing, but this alone is not a reason to sell.")
    steps.append("Before you buy, check the company's news and confirm it is available in your GoTrade account.")
    return {"status": label, "headline": headline, "steps": steps, "levels": levels_out,
            "reminder": "Prices can gap past a stop, so a loss can be bigger than planned. You make the final decision; this app never places trades."}

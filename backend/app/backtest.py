"""Event-driven backtest with next-bar execution and no look-ahead.

- A signal confirmed at the close of bar t is filled at the open of bar t+1,
  adjusted by slippage against the trade.
- Stops and targets are checked from the fill bar onward using each bar's
  high and low. If a bar touches both, the stop is assumed to fill first.
- If a bar opens beyond the stop (a gap), the exit fills at that open, so
  losses can exceed the planned risk.
- Trailing stops are recalculated at each completed close and apply from the
  next bar. Stops only ever tighten; they are never widened.
- One position at a time. Re-entry needs a new signal at least
  `reentry_cooldown_bars` after the previous exit.
- Signals on bars with insufficient history are skipped for every variant,
  so earlier variants cannot bypass the data-quality gate.
- Results are reported separately for the in-sample and the most recent
  out-of-sample portion of the data, with the same frozen rules.
"""
from __future__ import annotations

import math
from dataclasses import asdict

from .config import AnalysisConfig, BacktestConfig
from .sessions import Candle
from .structure import confirmed_by, swings

VARIANT_SIGNALS = {"early": ("breakout", "early"), "confirmed": ("breakout", "confirmed"), "retest": ("retest", "retest")}


def run(candles: list[Candle], ind: dict, signals: list[dict], acfg: AnalysisConfig, cfg: BacktestConfig) -> dict:
    data = [c for c in candles if c.complete]
    direction = "bullish" if cfg.side == "long" else "bearish"
    sign = 1 if cfg.side == "long" else -1
    kind, variant = VARIANT_SIGNALS[cfg.entry_variant]
    entries = [s for s in signals if s["kind"] == kind and s["variant"] == variant and s["direction"] == direction]
    all_swings = swings(data, acfg)
    slip = cfg.slippage_bps / 10000
    split_index = int(len(data) * (1 - cfg.out_of_sample_fraction))
    trades: list[dict] = []
    skipped: list[dict] = []
    next_allowed = 0
    for s in entries:
        t = s["index"]
        if t < next_allowed or t + 1 >= len(data):
            continue
        if t < acfg.min_bars_for_signals or ind["atr"][t] is None:
            skipped.append({"time": s["time"], "reason": "insufficient history at signal bar"})
            continue
        fill_bar = data[t + 1]
        entry = fill_bar.open * (1 + sign * slip)
        opposite = confirmed_by(all_swings, t, "low" if sign == 1 else "high")
        stop = opposite[-1].price if cfg.stop_mode == "swing" and opposite else None
        if stop is None or sign * (entry - stop) <= 0:
            stop = entry - sign * cfg.stop_atr * ind["atr"][t]
        risk_per_share = sign * (entry - stop)
        if risk_per_share <= 0:
            skipped.append({"time": s["time"], "reason": "no valid stop below entry"})
            continue
        shares = math.floor(min(cfg.risk_per_trade / risk_per_share, cfg.starting_capital / entry))
        if shares < 1:
            skipped.append({"time": s["time"], "reason": "risk budget buys less than one share"})
            continue
        trade = simulate(data, ind, t + 1, entry, stop, shares, sign, cfg)
        trade.update({"signal_time": s["time"], "signal_index": t, "entry_time": fill_bar.time, "initial_stop": stop, "risk_per_share": risk_per_share, "sample": "in" if t < split_index else "out"})
        trade["r_multiple"] = trade["net_pnl"] / (risk_per_share * shares)
        trade["premature"] = premature(data, trade, sign, cfg)
        trades.append(trade)
        next_allowed = trade["exit_index"] + cfg.reentry_cooldown_bars
    return {
        "config": asdict(cfg), "bars": len(data), "split_time": data[split_index].time if split_index < len(data) else None,
        "trades": trades, "skipped": skipped,
        "summary": {"all": summarize(trades, cfg), "in_sample": summarize([t for t in trades if t["sample"] == "in"], cfg), "out_of_sample": summarize([t for t in trades if t["sample"] == "out"], cfg)},
        "notes": ["Fills at the next candle's open after a completed signal candle.", "When a candle touches both stop and target, the stop is assumed first.", "Gaps beyond the stop fill at the open, which can exceed the planned loss.", "Past results on this data do not establish a probability of future outcomes."],
    }


def simulate(data: list[Candle], ind: dict, start: int, entry: float, stop: float, shares: int, sign: int, cfg: BacktestConfig) -> dict:
    slip = cfg.slippage_bps / 10000
    risk = sign * (entry - stop)
    target = entry + sign * cfg.target_r * risk if cfg.exit_mode == "full" else None
    partial_price = entry + sign * cfg.partial_r * risk if cfg.exit_mode == "partial" else None
    final_target = entry + sign * cfg.target_r * risk if cfg.exit_mode == "partial" else None
    remaining = shares
    fills: list[dict] = []
    fees = cfg.fee_per_order + cfg.fee_per_share * shares
    current_stop = stop

    def exit_fill(price: float, qty: int, reason: str, index: int) -> None:
        nonlocal remaining, fees
        price = price * (1 - sign * slip)
        fills.append({"price": price, "shares": qty, "reason": reason, "time": data[index].time, "index": index})
        fees += cfg.fee_per_order + cfg.fee_per_share * qty
        remaining -= qty

    i = start
    while i < len(data) and remaining > 0:
        bar = data[i]
        adverse = bar.low if sign == 1 else bar.high
        favorable = bar.high if sign == 1 else bar.low
        if sign * (bar.open - current_stop) <= 0:
            exit_fill(bar.open, remaining, "gap through stop", i)
            break
        if sign * (adverse - current_stop) <= 0:
            exit_fill(current_stop, remaining, "stop", i)
            break
        if partial_price is not None and sign * (favorable - partial_price) >= 0:
            qty = max(1, math.floor(shares * cfg.partial_fraction)) if remaining == shares else 0
            if qty and qty < remaining:
                exit_fill(partial_price, qty, "partial target", i)
                partial_price = None
                if cfg.move_stop_to_breakeven_after_partial:
                    current_stop = entry if sign * (entry - current_stop) > 0 else current_stop
        hit = target if target is not None else final_target if partial_price is None and final_target is not None else None
        if hit is not None and sign * (favorable - hit) >= 0:
            exit_fill(hit, remaining, "target", i)
            break
        if cfg.exit_mode == "trailing" and ind["atr"][i] is not None:
            trail = bar.close - sign * cfg.trail_atr * ind["atr"][i]
            if sign * (trail - current_stop) > 0:
                current_stop = trail  # tighten only
        i += 1
    if remaining > 0:
        last = len(data) - 1
        fills.append({"price": data[last].close, "shares": remaining, "reason": "open at end of data (marked to last close)", "time": data[last].time, "index": last})
        remaining = 0
    gross = sum(sign * (f["price"] - entry) * f["shares"] for f in fills)
    return {"entry_price": entry, "shares": shares, "fills": fills, "exit_index": fills[-1]["index"], "exit_reason": fills[-1]["reason"], "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees, "final_stop": current_stop}


def premature(data: list[Candle], trade: dict, sign: int, cfg: BacktestConfig) -> dict:
    """After the final exit, how far did price keep going in the trade's favor?"""
    i = trade["exit_index"]
    window = data[i + 1:i + 1 + cfg.premature_exit_lookahead]
    if not window:
        return {"evaluated": False, "reason": "no candles after exit"}
    exit_price = trade["fills"][-1]["price"]
    best = max(sign * ((c.high if sign == 1 else c.low) - exit_price) for c in window)
    risk = trade["risk_per_share"]
    recovered = any(sign * (c.close - trade["entry_price"]) > 0 for c in window)
    return {"evaluated": True, "bars": len(window), "further_favorable_r": best / risk, "continued_one_r": best >= risk, "stopped_then_recovered": trade["exit_reason"] in ("stop", "gap through stop") and recovered}


def summarize(trades: list[dict], cfg: BacktestConfig) -> dict:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t["net_pnl"] > 0]
    gains = sum(t["net_pnl"] for t in wins)
    losses = -sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0)
    equity, peak, drawdown = cfg.starting_capital, cfg.starting_capital, 0.0
    for t in trades:
        equity += t["net_pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(trades), "wins": len(wins), "win_rate": len(wins) / len(trades),
        "net_pnl": sum(t["net_pnl"] for t in trades), "fees": sum(t["fees"] for t in trades),
        "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
        "profit_factor": gains / losses if losses else None, "max_drawdown": drawdown,
        "losses_beyond_planned_risk": sum(1 for t in trades if t["r_multiple"] < -1.0001),
        "premature_exits": sum(1 for t in trades if t["premature"].get("continued_one_r")),
        "stopped_then_recovered": sum(1 for t in trades if t["premature"].get("stopped_then_recovered")),
    }

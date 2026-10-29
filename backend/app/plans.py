"""Entry, exit, and projection calculations for a user's trade plan.

User-entered levels and algorithm-derived levels are kept in separate fields
and labeled by source. Nothing here places or suggests placing an order; the
user decides.
"""
from __future__ import annotations

import math


class PlanError(ValueError):
    pass


def _pnl(sign: int, entry: float, exit_price: float, shares: float, fees: float) -> float:
    return sign * (exit_price - entry) * shares - fees


def calculate(plan: dict, algo_levels: dict | None) -> dict:
    side = plan.get("side", "long")
    sign = 1 if side == "long" else -1
    entry = float(plan["entry"])
    stop = plan.get("stop")
    if entry <= 0:
        raise PlanError("Entry price must be above zero.")
    if stop is None:
        raise PlanError("Enter a stop (invalidation) price. Use an algorithm-derived level or your own.")
    stop = float(stop)
    risk_per_share = sign * (entry - stop)
    if risk_per_share <= 0:
        raise PlanError(f"For a {side} plan the stop must be {'below' if sign == 1 else 'above'} the entry.")
    original = plan.get("original_stop")
    if plan.get("mode") == "existing" and original is not None and sign * (float(original) - stop) > 1e-9:
        raise PlanError(f"The stop cannot be widened from {float(original):.4f} to {stop:.4f} on an existing position. You can tighten it or exit.")
    slip = float(plan.get("slippage_bps", 5)) / 10000
    fee_share, fee_order = float(plan.get("fee_per_share", 0)), float(plan.get("fee_per_order", 0))
    capital = float(plan.get("capital") or 0)
    max_risk = float(plan.get("max_risk") or 0)
    by_risk = math.floor(max_risk / risk_per_share) if max_risk else None
    by_capital = math.floor(capital / entry) if capital else None
    if plan.get("shares"):
        shares = int(plan["shares"])
    else:
        limits = [x for x in (by_risk, by_capital) if x is not None]
        shares = min(limits) if limits else 0
    if shares <= 0:
        raise PlanError("Enter shares, or a capital amount and maximum risk that allow at least one share.")
    fill_entry = entry * (1 + sign * slip)
    round_trip_fees = 2 * fee_order + 2 * fee_share * shares
    stop_fill = stop * (1 - sign * slip)
    loss_at_stop = _pnl(sign, fill_entry, stop_fill, shares, round_trip_fees)
    warnings = ["A gap or fast market can fill beyond the stop. The loss at stop below assumes the stop fills at its price plus slippage."]
    if max_risk and -loss_at_stop > max_risk:
        warnings.append(f"Planned loss at stop ({-loss_at_stop:.2f}) exceeds your maximum risk ({max_risk:.2f}).")
    if capital and shares * entry > capital:
        warnings.append("Position cost exceeds available capital.")

    targets = []
    for t in plan.get("targets") or []:
        if t.get("price"):
            targets.append({"price": float(t["price"]), "source": "user", "label": t.get("label", "User target")})
    if algo_levels:
        for p in (algo_levels.get("swing_highs_above") if sign == 1 else algo_levels.get("swing_lows_below")) or []:
            targets.append({"price": p, "source": "algorithm", "label": "Prior confirmed swing " + ("high" if sign == 1 else "low")})
        mm = algo_levels.get("measured_move_up" if sign == 1 else "measured_move_down")
        if mm:
            targets.append({"price": mm, "source": "algorithm", "label": "Measured move"})
    for r in (1.0, 2.0):
        targets.append({"price": entry + sign * r * risk_per_share, "source": "calculated", "label": f"{r:g}R from entry and stop"})
    targets = [t for t in targets if sign * (t["price"] - entry) > 0]
    targets.sort(key=lambda t: sign * t["price"])
    for t in targets:
        exit_fill = t["price"] * (1 - sign * slip)
        t["pnl"] = _pnl(sign, fill_entry, exit_fill, shares, round_trip_fees)
        t["reward_to_risk"] = t["pnl"] / -loss_at_stop if loss_at_stop < 0 else None

    exits = []
    if targets:
        t1 = targets[0]
        t2 = targets[1] if len(targets) > 1 else targets[0]
        half = shares // 2
        exits.append({"rule": f"Full exit at {t2['price']:.4f} ({t2['label']}, {t2['source']})", "pnl_if_reached": t2["pnl"], "pnl_if_stopped": loss_at_stop})
        if half >= 1:
            part_fees = 3 * fee_order + 2 * fee_share * shares
            pnl_partial = sign * (t1["price"] * (1 - sign * slip) - fill_entry) * half + sign * (t2["price"] * (1 - sign * slip) - fill_entry) * (shares - half) - part_fees
            be_case = sign * (t1["price"] * (1 - sign * slip) - fill_entry) * half + sign * (fill_entry * (1 - sign * slip) - fill_entry) * (shares - half) - part_fees
            exits.append({"rule": f"Sell {half} at {t1['price']:.4f}, move stop to entry, rest at {t2['price']:.4f}", "pnl_if_reached": pnl_partial, "pnl_if_stopped_after_partial": be_case, "pnl_if_stopped": loss_at_stop})
        exits.append({"rule": "Trailing stop: after each completed candle, raise the stop to close minus 2 ATR (never lower it)", "pnl_if_reached": None, "pnl_if_stopped": loss_at_stop, "note": "The result depends on the path. Worst case is the initial stop; the stop only tightens."})
    return {
        "side": side, "mode": plan.get("mode", "plan"), "shares": shares, "shares_by_risk": by_risk, "shares_by_capital": by_capital,
        "position_cost": shares * fill_entry, "risk_per_share": risk_per_share, "loss_at_stop": loss_at_stop,
        "round_trip_fees": round_trip_fees, "entry_with_slippage": fill_entry,
        "user_levels": {"entry": entry, "stop": stop, "targets": [t for t in targets if t["source"] == "user"]},
        "algorithm_levels": algo_levels or {}, "targets": targets, "exit_comparison": exits, "warnings": warnings,
        "reminder": "These figures are conditional calculations, not forecasts. You decide whether and when to trade. This app never places orders.",
    }


def outcome(plan: dict, actual_exit: float | None, actual_shares: int | None) -> dict | None:
    if actual_exit is None:
        return None
    sign = 1 if plan.get("side", "long") == "long" else -1
    entry = float(plan.get("actual_entry") or plan["entry"])
    shares = int(actual_shares or plan.get("shares") or 0)
    planned_risk = sign * (float(plan["entry"]) - float(plan["stop"])) * shares
    pnl = sign * (float(actual_exit) - entry) * shares
    return {"actual_pnl": pnl, "planned_risk": planned_risk, "actual_r": pnl / planned_risk if planned_risk else None}

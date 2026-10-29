import pytest

from app import backtest, plans
from app.config import AnalysisConfig, BacktestConfig
from app.sessions import Candle


def c(i, o, h, l, cl):
    return Candle(i * 60, i * 60 + 60, o, h, l, cl, 100, "regular", "2026-10-26", i, 1, True)


def setup(bars):
    ind = {"atr": [1.0] * len(bars)}
    signal = {"kind": "breakout", "variant": "confirmed", "direction": "bullish", "index": 0, "time": 0, "level": 10}
    return ind, [signal]


def test_entry_fills_at_next_bar_open_with_slippage():
    bars = [c(0, 10, 10, 10, 10), c(1, 11, 11.2, 10.9, 11), c(2, 11, 14, 11, 13)]
    ind, sig = setup(bars)
    cfg = BacktestConfig(stop_mode="atr", stop_atr=1.0, slippage_bps=10, target_r=2.0, risk_per_trade=100)
    out = backtest.run(bars, ind, sig, AnalysisConfig(min_bars_for_signals=0), cfg)
    trade = out["trades"][0]
    assert trade["entry_time"] == 60
    assert trade["entry_price"] == pytest.approx(11 * 1.001)
    assert trade["exit_reason"] == "target"


def test_gap_through_stop_fills_at_open_and_loses_more_than_planned():
    bars = [c(0, 10, 10, 10, 10), c(1, 11, 11.1, 10.9, 11), c(2, 8, 8.5, 7.5, 8)]
    ind, sig = setup(bars)
    cfg = BacktestConfig(stop_mode="atr", stop_atr=1.0, slippage_bps=0, risk_per_trade=100)
    trade = backtest.run(bars, ind, sig, AnalysisConfig(min_bars_for_signals=0), cfg)["trades"][0]
    assert trade["exit_reason"] == "gap through stop" and trade["fills"][-1]["price"] == 8
    assert trade["r_multiple"] < -1


def test_stop_assumed_first_when_bar_touches_stop_and_target():
    bars = [c(0, 10, 10, 10, 10), c(1, 11, 11.1, 10.95, 11), c(2, 11, 20, 5, 11)]
    ind, sig = setup(bars)
    trade = backtest.run(bars, ind, sig, AnalysisConfig(min_bars_for_signals=0), BacktestConfig(stop_mode="atr", slippage_bps=0))["trades"][0]
    assert trade["exit_reason"] == "stop"


def test_trailing_stop_only_tightens():
    bars = [c(0, 10, 10, 10, 10), c(1, 11, 11.1, 10.9, 11), c(2, 11, 13, 11, 13), c(3, 13, 13, 12, 12), c(4, 12, 12, 10.5, 11)]
    ind, sig = setup(bars)
    cfg = BacktestConfig(stop_mode="atr", exit_mode="trailing", trail_atr=1.0, slippage_bps=0)
    trade = backtest.run(bars, ind, sig, AnalysisConfig(min_bars_for_signals=0), cfg)["trades"][0]
    # Stop rises to 12 after bar 2 closes at 13 and stays there; bar 3 low 12 hits it.
    assert trade["exit_reason"] == "stop" and trade["fills"][-1]["price"] == 12


def test_insufficient_history_blocks_every_variant():
    bars = [c(0, 10, 10, 10, 10), c(1, 11, 11.1, 10.9, 11), c(2, 11, 14, 11, 13)]
    ind, sig = setup(bars)
    out = backtest.run(bars, ind, sig, AnalysisConfig(min_bars_for_signals=60), BacktestConfig(stop_mode="atr"))
    assert not out["trades"] and out["skipped"]


def test_plan_sizes_by_risk_and_capital():
    out = plans.calculate({"side": "long", "entry": 10, "stop": 9, "max_risk": 100, "capital": 500, "slippage_bps": 0}, None)
    assert out["shares_by_risk"] == 100 and out["shares_by_capital"] == 50 and out["shares"] == 50
    assert out["loss_at_stop"] == pytest.approx(-50)
    assert {t["label"] for t in out["targets"]} >= {"1R from entry and stop", "2R from entry and stop"}


def test_existing_position_stop_cannot_be_widened():
    with pytest.raises(plans.PlanError, match="cannot be widened"):
        plans.calculate({"mode": "existing", "side": "long", "entry": 10, "stop": 8.5, "original_stop": 9, "shares": 10}, None)
    assert plans.calculate({"mode": "existing", "side": "long", "entry": 10, "stop": 9.5, "original_stop": 9, "shares": 10}, None)["shares"] == 10


def test_user_and_algorithm_levels_are_labeled_separately():
    out = plans.calculate({"side": "long", "entry": 10, "stop": 9, "shares": 10, "targets": [{"price": 12.5}]}, {"swing_highs_above": [11.2], "measured_move_up": 13})
    sources = {(t["price"], t["source"]) for t in out["targets"]}
    assert (12.5, "user") in sources and (11.2, "algorithm") in sources and (13, "algorithm") in sources

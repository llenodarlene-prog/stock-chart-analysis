"""Next Trading Day Watchlist scanner.

Universe: the official GoTrade US stock directory
(https://www.heygotrade.com/en/us-stock/), stored with its HTTP Last-Modified
date and fetch time. Account availability is a manual confirmation by the user.
Alpaca tradability is never used as a substitute.

Data: regular-session 30-minute IEX bars from Alpaca. Daily candles are built
from those bars, so the scan, the charts, and both backtests share one dataset.

Setups are evaluated on completed daily candles only (rules in ScanConfig):

developing_breakout (can be quiet; no gain or volume surge required)
  - latest confirmed swing high R exists and 0 <= R - close <= near_resistance_atr x ATR
  - close > EMA 20 and the latest confirmed swing low holds
  - the 5-day average range <= contraction_ratio x ATR (tightening)
momentum_continuation
  - swing trend is up and close > EMA 9 > EMA 20
  - close is in the top `close_strength` of the day's range and relative volume >= momentum_min_rvol
  - extension (close - EMA 20) / ATR is reported, never used to reject;
    it sets the stop distance and therefore the size
uptrend_pullback
  - swing trend is up and the latest swing low holds
  - the 10-day high is at least pullback_min_atr x ATR above the close
  - close is within pullback_ema_band ATR of EMA 20

Each candidate gets two separate plans: an overnight-to-next-opening-hour plan
and a multi-day swing plan. Ranking is an ordering by the count of optional
confirmations met, never a probability.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .alpaca import AlpacaClient, AlpacaError, Bar
from .config import AnalysisConfig
from .indicators import compute_all
from .sessions import NY, Candle, aggregate, session_of
from .store import Store
from .structure import confirmed_by, swings, trend_at

DIRECTORY_URL = "https://www.heygotrade.com/en/us-stock/"


@dataclass(frozen=True)
class ScanConfig:
    lookback_days: int = 180
    min_history_days: int = 60
    min_price: float = 1.0
    min_iex_dollar_volume: float = 100_000.0  # 20-day average, IEX trades only
    max_spread_pct: float = 1.0
    near_resistance_atr: float = 0.75
    contraction_ratio: float = 0.9
    momentum_min_rvol: float = 1.2
    close_strength: float = 0.25
    pullback_min_atr: float = 1.0
    pullback_ema_band: float = 1.0
    max_candidates: int = 10
    max_per_setup: int = 4
    news_days: int = 3
    swing_max_hold_days: int = 10
    swing_target_r: float = 2.0
    overnight_exit: str = "10:30"  # New York time on the next session
    slippage_bps: float = 10.0
    out_of_sample_fraction: float = 0.3


# Universe ----------------------------------------------------------------------

def parse_directory(html: str) -> list[tuple[str, str]]:
    """Every ticker linked from the directory. The ticker comes from the link
    path; the name is the link text without a trailing "(TICKER)"."""
    import html as htmllib

    rows: dict[str, str] = {}
    for slug, text in re.findall(r'href="/en/us-stock/([a-z0-9.\-]+)/"[^>]*>((?:[^<]|<(?!/a>))*)</a>', html):
        symbol = slug.upper()
        name = htmllib.unescape(re.sub(r"<[^>]+>", " ", text)).strip()
        name = re.sub(r"\s+", " ", re.sub(rf"\s*\({re.escape(symbol)}\)\s*$", "", name, flags=re.I))
        if name or symbol not in rows:
            rows[symbol] = name
    return sorted(rows.items())


def refresh_universe(store: Store, transport: httpx.BaseTransport | None = None) -> dict:
    with httpx.Client(timeout=30, transport=transport, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (personal watchlist scanner)"}) as http:
        response = http.get(DIRECTORY_URL)
    if response.status_code != 200:
        raise AlpacaError(f"GoTrade directory returned HTTP {response.status_code}. The previous universe was kept.")
    rows = parse_directory(response.text)
    if len(rows) < 100:
        raise AlpacaError(f"GoTrade directory parsed to only {len(rows)} symbols; the page layout may have changed. The previous universe was kept.")
    store.replace_universe(rows, DIRECTORY_URL, response.headers.get("last-modified"))
    return {"symbols": len(rows), "source_url": DIRECTORY_URL, "source_last_modified": response.headers.get("last-modified"), "fetched_at": time.time()}


# Data -----------------------------------------------------------------------------

def ensure_30m(client: AlpacaClient, store: Store, symbols: list[str], cfg: ScanConfig, now: int, batch: int = 100) -> list[str]:
    """Fetches missing and recent 30-minute bars. Returns error messages."""
    start = now - cfg.lookback_days * 86400
    coverage = store.coverage_30m(client.settings.feed)
    errors: list[str] = []
    fresh = [s for s in symbols if s not in coverage or coverage[s]["start"] > start]
    stale = [s for s in symbols if s in coverage and coverage[s]["start"] <= start and now - coverage[s]["fetched_at"] > 600]
    for group, begin in ((fresh, start), (stale, None)):
        for i in range(0, len(group), batch):
            chunk = group[i:i + batch]
            frm = begin if begin is not None else min(coverage[s]["end"] for s in chunk) - 2 * 86400
            try:
                data = client.bars_multi(chunk, "30Min", frm, now)
            except AlpacaError as exc:
                errors.append(f"{chunk[0]}…{chunk[-1]}: {exc}")
                continue
            for symbol, bars in data.items():
                store.upsert_bars_30m(symbol, client.settings.feed, bars)
            store.set_coverage_30m(chunk, client.settings.feed, frm, now)
    return errors


def daily_candles(bars: list[Bar], now: int) -> list[Candle]:
    return aggregate(bars, "1Day", {"regular"}, now)


# Setups ---------------------------------------------------------------------------

def evaluate(candles: list[Candle], acfg: AnalysisConfig, cfg: ScanConfig, t: int | None = None, ind: dict | None = None, all_swings=None) -> list[dict]:
    """Setups present at completed daily candle t (defaults to the last one)."""
    done = [c for c in candles if c.complete]
    if t is None:
        t = len(done) - 1
    if t < cfg.min_history_days:
        return []
    ind = ind or compute_all(done, acfg, daily=True)
    all_swings = all_swings if all_swings is not None else swings(done, acfg)
    c = done[t]
    atr, ema9, ema20, rsi, rvol = (ind[k][t] for k in ("atr", "ema_fast", "ema_slow", "rsi", "rvol"))
    if atr is None or ema20 is None or ema9 is None or atr <= 0:
        return []
    trend = trend_at(all_swings, t)
    highs, lows = confirmed_by(all_swings, t, "high"), confirmed_by(all_swings, t, "low")
    R = highs[-1].price if highs else None
    S = lows[-1].price if lows else None
    swing_low_holds = S is not None and c.close > S
    avg_range5 = sum(x.high - x.low for x in done[t - 4:t + 1]) / 5
    extension = (c.close - ema20) / atr
    day_range = c.high - c.low
    close_pos = (c.close - c.low) / day_range if day_range > 0 else 0.5
    hi10 = max(x.high for x in done[t - 9:t + 1])
    gain = (c.close / done[t - 1].close - 1) * 100
    base = {"index": t, "time": c.time, "date": c.session_date, "close": c.close, "atr": atr, "ema9": ema9, "ema20": ema20, "rsi": rsi, "rvol": rvol, "trend": trend["direction"], "resistance": R, "support": S, "extension_atr": extension, "day_gain_pct": gain}
    found = []
    if R is not None and 0 <= R - c.close <= cfg.near_resistance_atr * atr and c.close > ema20 and swing_low_holds and avg_range5 <= cfg.contraction_ratio * atr:
        optional = {"relative volume >= 1": rvol is not None and rvol >= 1, "trend up": trend["direction"] == "up", "EMA 9 above EMA 20": ema9 > ema20}
        found.append({**base, "setup": "developing_breakout", "quiet": not (rvol is not None and rvol >= 1.5) and abs(gain) < 3,
                      "evidence": [f"Close {c.close:.2f} is {R - c.close:.2f} ({(R - c.close) / atr:.2f} ATR) below confirmed resistance {R:.2f}.", f"5-day average range {avg_range5:.2f} is {avg_range5 / atr:.2f} x ATR (tightening).", f"Close is above EMA 20 ({ema20:.2f}) and swing low {S:.2f} holds."],
                      "optional": optional, "trigger_level": R, "stop": S})
    if trend["direction"] == "up" and c.close > ema9 > ema20 and close_pos >= 1 - cfg.close_strength and rvol is not None and rvol >= cfg.momentum_min_rvol:
        stop = max(x for x in (S, ema20) if x is not None and x < c.close) if any(x is not None and x < c.close for x in (S, ema20)) else c.close - 2 * atr
        optional = {"new 20-day closing high": c.close >= max(x.close for x in done[t - 19:t + 1]), "relative volume >= 2": rvol >= 2, "extension under 3 ATR": extension < 3}
        found.append({**base, "setup": "momentum_continuation", "quiet": False,
                      "evidence": [f"Swing trend up; close {c.close:.2f} > EMA 9 {ema9:.2f} > EMA 20 {ema20:.2f}.", f"Closed in the top {100 - close_pos * 100:.0f}% of the day's range with relative volume {rvol:.2f}x.", f"Extension: {extension:.2f} ATR above EMA 20 (reported, not a rejection). RSI {rsi:.1f} is informational only." if rsi is not None else f"Extension: {extension:.2f} ATR above EMA 20."],
                      "optional": optional, "trigger_level": c.high, "stop": stop})
    if trend["direction"] == "up" and swing_low_holds and hi10 - c.close >= cfg.pullback_min_atr * atr and abs(extension) <= cfg.pullback_ema_band:
        optional = {"closed up on the day": c.close > c.open, "relative volume below 1 (quiet pullback)": rvol is not None and rvol < 1, "EMA 9 above EMA 20": ema9 > ema20}
        found.append({**base, "setup": "uptrend_pullback", "quiet": True,
                      "evidence": [f"Swing trend up and swing low {S:.2f} holds.", f"Pulled back {(hi10 - c.close) / atr:.2f} ATR from the 10-day high {hi10:.2f}.", f"Close is {extension:+.2f} ATR from EMA 20 ({ema20:.2f})."],
                      "optional": optional, "trigger_level": c.high, "stop": S})
    return found


def plans_for(setup: dict, cfg: ScanConfig, acfg: AnalysisConfig, done: list[Candle], all_swings) -> dict:
    """Separate overnight and swing plans built from observed levels."""
    t, atr, close = setup["index"], setup["atr"], setup["close"]
    buffer = acfg.breakout_buffer_atr * atr
    stop = setup["stop"] if setup["stop"] is not None and setup["stop"] < close else close - 2 * atr
    higher = sorted({s.price for s in confirmed_by(all_swings, t, "high") if s.price > close})
    trigger = setup["trigger_level"] + buffer
    risk = trigger - stop
    targets = [{"price": p, "source": "prior confirmed swing high"} for p in higher[:2]]
    if setup["resistance"] is not None and setup["support"] is not None and setup["resistance"] > setup["support"]:
        targets.append({"price": setup["resistance"] + (setup["resistance"] - setup["support"]), "source": "measured move"})
    targets.append({"price": trigger + cfg.swing_target_r * risk, "source": f"{cfg.swing_target_r:g}R from trigger and invalidation"})
    targets = sorted([x for x in targets if x["price"] > trigger], key=lambda x: x["price"])
    swing = {
        "horizon": "multi-day swing",
        "entry_trigger": f"A completed daily candle closes above {trigger:.2f} ({setup['trigger_level']:.2f} + {acfg.breakout_buffer_atr} ATR). Fill assumption: next session open.",
        "invalidation": f"A completed daily candle closes below {stop:.2f}. A gap below it can fill lower.",
        "stop": stop, "trigger": trigger, "risk_per_share": risk, "targets": targets,
        "time_stop": f"Exit at the close of day {cfg.swing_max_hold_days} if neither stop nor target is hit.",
    }
    overnight_stop = max(stop, setup["ema9"]) if setup["ema9"] < close else stop
    overnight = {
        "horizon": f"overnight to next opening hour (exit by {cfg.overnight_exit} ET)",
        "entry_trigger": f"Setup confirmed at today's close {close:.2f}. Entry only if the last 30 minutes of today hold above {overnight_stop:.2f}; otherwise skip.",
        "invalidation": f"Next session opens at or below {overnight_stop:.2f}, or a 30-minute candle before {cfg.overnight_exit} trades below it.",
        "stop": overnight_stop, "trigger": close, "risk_per_share": close - overnight_stop,
        "targets": [{"price": close + (close - overnight_stop), "source": "1R from close and invalidation"}] + [x for x in targets if x["source"] == "prior confirmed swing high"][:1],
        "time_stop": f"Exit at {cfg.overnight_exit} ET next session regardless of result.",
        "gap_note": "Overnight holds carry gap risk: news before the open can move the first price far beyond the stop.",
    }
    return {"swing": swing, "overnight": overnight}


# Liquidity, catalysts, and ranking -------------------------------------------------

def liquidity(done: list[Candle], snapshot: dict | None, cfg: ScanConfig) -> dict:
    last20 = done[-20:]
    dollar = sum(c.close * c.volume for c in last20) / len(last20) if last20 else 0
    quote = (snapshot or {}).get("latestQuote") or {}
    ap, bp = quote.get("ap") or 0, quote.get("bp") or 0
    spread = (ap - bp) / ((ap + bp) / 2) * 100 if ap > 0 and bp > 0 and ap >= bp else None
    issues = []
    if dollar < cfg.min_iex_dollar_volume:
        issues.append(f"20-day average IEX dollar volume {dollar:,.0f} is below {cfg.min_iex_dollar_volume:,.0f}.")
    if spread is None:
        issues.append("No valid IEX quote for a spread estimate.")
    elif spread > cfg.max_spread_pct:
        issues.append(f"IEX quoted spread {spread:.2f}% exceeds {cfg.max_spread_pct}%.")
    return {"iex_avg_dollar_volume": dollar, "spread_pct": spread, "quote_time": quote.get("t"), "ok": not issues, "issues": issues, "note": "IEX-only volume and quotes; consolidated liquidity is higher. Spread and slippage are execution costs."}


def catalysts(articles: list[dict], symbol: str) -> dict:
    mine = [a for a in articles if symbol in (a.get("symbols") or [])]
    return {
        "articles": mine[:5],
        "status": "sourced" if mine else "none found",
        "note": "Articles come from Alpaca's news API with source, time and link. Their presence is verified; their content and impact are not judged. Open the source before relying on it." if mine else "No article in the news API for this window. No catalyst is assumed.",
    }


def rank_key(candidate: dict) -> tuple:
    met = sum(1 for v in candidate["optional"].values() if v)
    return (-met, -int(candidate["catalyst"]["status"] == "sourced"), -(candidate["liquidity"]["iex_avg_dollar_volume"] or 0))


def select(candidates: list[dict], cfg: ScanConfig) -> list[dict]:
    """Up to max_candidates, at most max_per_setup per setup so quiet setups are
    not crowded out by momentum names. Fewer are returned when fewer qualify."""
    chosen, per = [], {}
    for c in sorted(candidates, key=rank_key):
        if per.get(c["setup"], 0) >= cfg.max_per_setup:
            continue
        chosen.append(c)
        per[c["setup"]] = per.get(c["setup"], 0) + 1
        if len(chosen) >= cfg.max_candidates:
            break
    for i, c in enumerate(chosen, 1):
        c["rank"] = i
        c["rank_note"] = f"Ordering by optional confirmations met ({sum(1 for v in c['optional'].values() if v)} of {len(c['optional'])}), then sourced news, then liquidity. Not a probability."
    return chosen


def run_scan(client: AlpacaClient, store: Store, acfg: AnalysisConfig, cfg: ScanConfig, now: int, symbols: list[str] | None = None) -> dict:
    universe = store.universe()
    if not universe:
        raise AlpacaError("The GoTrade universe has not been loaded. Refresh it first.")
    names = {u["symbol"]: u["name"] for u in universe}
    chosen = [s for s in (symbols or list(names)) if s in names]
    errors = ensure_30m(client, store, chosen, cfg, now)
    start = now - cfg.lookback_days * 86400
    found: list[dict] = []
    rejected = {"insufficient_history": 0, "price_below_min": 0, "no_setup": 0, "stale": 0}
    last_session = None
    per_symbol = {}
    for symbol in chosen:
        done = [c for c in daily_candles(store.bars_30m(symbol, client.settings.feed, start), now) if c.complete]
        if len(done) < cfg.min_history_days:
            rejected["insufficient_history"] += 1
            continue
        last_session = max(last_session or "", done[-1].session_date)
        per_symbol[symbol] = done
    for symbol, done in per_symbol.items():
        if done[-1].session_date != last_session:
            rejected["stale"] += 1
            continue
        if done[-1].close < cfg.min_price:
            rejected["price_below_min"] += 1
            continue
        ind = compute_all(done, acfg, daily=True)
        sw = swings(done, acfg)
        setups = evaluate(done, acfg, cfg, ind=ind, all_swings=sw)
        if not setups:
            rejected["no_setup"] += 1
        for s in setups:
            found.append({**s, "symbol": symbol, "name": names.get(symbol, ""), "plans": plans_for(s, cfg, acfg, done, sw), "history_days": len(done)})
    # Liquidity and news only for symbols that have a setup.
    syms = sorted({c["symbol"] for c in found})
    snaps, articles = {}, []
    for i in range(0, len(syms), 100):
        try:
            snaps.update(client.snapshots(syms[i:i + 100]))
        except AlpacaError as exc:
            errors.append(f"Snapshots: {exc}")
    since = datetime.fromtimestamp(now - cfg.news_days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(0, len(syms), 50):
        try:
            articles += client.news(syms[i:i + 50], since)
        except AlpacaError as exc:
            errors.append(f"News: {exc}")
    availability = store.availability()
    qualified, held_back = [], []
    for c in found:
        c["liquidity"] = liquidity(per_symbol[c["symbol"]], snaps.get(c["symbol"]), cfg)
        c["catalyst"] = catalysts(articles, c["symbol"])
        c["account_availability"] = availability.get(c["symbol"]) or {"available": None, "note": "Not confirmed. Check the GoTrade app and confirm manually."}
        c["data_quality"] = {"ok": True, "last_session": last_session, "history_days": c["history_days"], "issues": []}
        (qualified if c["liquidity"]["ok"] else held_back).append(c)
    picks = select(qualified, cfg)
    for c in picks:
        c.pop("index", None)
    source = universe[0]
    result = {
        "created_at": now, "last_session": last_session, "feed": client.settings.feed,
        "universe": {"source_url": source.get("source_url"), "source_last_modified": source.get("source_last_modified"), "fetched_at": source.get("fetched_at"), "symbols": len(universe), "scanned": len(chosen)},
        "candidates": picks, "qualified_count": len(qualified), "held_back_for_liquidity": len(held_back), "rejected": rejected, "errors": errors,
        "config": asdict(cfg),
        "notes": ["Fewer than 10 candidates are returned when fewer meet every rule.", "Rank is an ordering, not a probability. No move size is promised.", "Strong momentum is not rejected for a high daily gain or RSI; extension sets the stop distance and size instead.", "This scanner does not place trades."],
    }
    return result


# Reassessment after news and opening gaps ---------------------------------------------

def reassess(scan: dict, client: AlpacaClient, now: int) -> dict:
    syms = [c["symbol"] for c in scan["candidates"]]
    if not syms:
        return scan
    snaps = client.snapshots(syms)
    since = datetime.fromtimestamp(scan["created_at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        fresh_news = client.news(syms, since)
    except AlpacaError:
        fresh_news = None
    for c in scan["candidates"]:
        snap = snaps.get(c["symbol"]) or {}
        trade = snap.get("latestTrade") or {}
        price, at = trade.get("p"), trade.get("t")
        notes, status = [], "plan unchanged"
        if price is None:
            status, notes = "no current price", ["No recent IEX trade. Reassess after the open."]
        else:
            gap = (price / c["close"] - 1) * 100
            notes.append(f"Latest IEX trade {price:.2f} at {at} is {gap:+.2f}% from the scan close {c['close']:.2f}.")
            for key in ("overnight", "swing"):
                plan = c["plans"][key]
                if price <= plan["stop"]:
                    notes.append(f"{plan['horizon']}: price is at or below invalidation {plan['stop']:.2f}. The plan is invalidated before entry.")
                    status = "invalidated by gap"
                elif key == "swing" and price > plan["trigger"] + c["atr"]:
                    notes.append(f"{plan['horizon']}: price is more than 1 ATR above the trigger. Risk to invalidation is now {price - plan['stop']:.2f} per share; re-plan with the opening range instead of chasing.")
                    if status == "plan unchanged":
                        status = "extended: re-plan"
        if fresh_news is None:
            notes.append("News check failed; review news manually.")
        else:
            known = {a.get("id") for a in c["catalyst"]["articles"]}
            new = [a for a in fresh_news if c["symbol"] in (a.get("symbols") or []) and a.get("id") not in known and (a.get("created_at") or "") > since]
            if new:
                notes.append(f"{len(new)} new article(s) since the scan. Review them before acting.")
                c["catalyst"]["articles"] = (new + c["catalyst"]["articles"])[:5]
                c["catalyst"]["status"] = "sourced"
                if status == "plan unchanged":
                    status = "new news: review"
        c["reassessment"] = {"at": now, "status": status, "notes": notes}
    scan["reassessed_at"] = now
    return scan


# Backtests -------------------------------------------------------------------------------

def backtest(store: Store, feed: str, symbols: list[str], acfg: AnalysisConfig, cfg: ScanConfig, now: int) -> dict:
    """Overnight and swing horizons, each setup tested independently.
    Setups are detected with data up to each day only."""
    start = now - cfg.lookback_days * 86400
    trades = {"overnight": [], "swing": []}
    exit_h, exit_m = (int(x) for x in cfg.overnight_exit.split(":"))
    slip = cfg.slippage_bps / 10000
    all_dates = set()
    for symbol in symbols:
        bars = store.bars_30m(symbol, feed, start)
        done = [c for c in daily_candles(bars, now) if c.complete]
        if len(done) <= cfg.min_history_days + 1:
            continue
        ind = compute_all(done, acfg, daily=True)
        sw = swings(done, acfg)
        by_date: dict[str, list[Bar]] = {}
        for b in bars:
            found = session_of(b.t)
            if found and found[0] == "regular":
                by_date.setdefault(found[1].date().isoformat(), []).append(b)
        for t in range(cfg.min_history_days, len(done) - 1):
            for s in evaluate(done, acfg, cfg, t=t, ind=ind, all_swings=sw):
                all_dates.add(done[t].session_date)
                p = plans_for(s, cfg, acfg, done, sw)
                # Overnight: enter at today's close, exit by the configured time next session.
                o = p["overnight"]
                nxt = by_date.get(done[t + 1].session_date, [])
                if nxt and o["risk_per_share"] > 0:
                    entry = done[t].close * (1 + slip)
                    exit_price, reason = None, "time exit"
                    cutoff = datetime.combine(datetime.fromisoformat(done[t + 1].session_date).date(), datetime.min.time(), NY).replace(hour=exit_h, minute=exit_m).timestamp()
                    if nxt[0].o <= o["stop"]:
                        exit_price, reason = nxt[0].o, "gap through invalidation"
                    else:
                        window = [b for b in nxt if b.t < cutoff]
                        for b in window:
                            if b.l <= o["stop"]:
                                exit_price, reason = o["stop"], "stop"
                                break
                        if exit_price is None and window:
                            exit_price = window[-1].c
                    if exit_price is not None:
                        exit_price *= 1 - slip
                        trades["overnight"].append(_trade(symbol, s, done[t].session_date, entry, exit_price, entry - o["stop"], reason, cfg))
                # Swing: enter next open if today's close already clears the trigger.
                sp = p["swing"]
                if done[t].close > sp["trigger"] and sp["risk_per_share"] > 0:
                    entry = done[t + 1].open * (1 + slip)
                    stop, target = sp["stop"], entry + cfg.swing_target_r * (entry - sp["stop"])
                    exit_price, reason = None, "time exit"
                    for k in range(t + 1, min(t + 1 + cfg.swing_max_hold_days, len(done))):
                        d = done[k]
                        if k > t + 1 and d.open <= stop:
                            exit_price, reason = d.open, "gap through invalidation"
                            break
                        if d.low <= stop:
                            exit_price, reason = min(stop, d.open), "stop"
                            break
                        if d.high >= target:
                            exit_price, reason = target, "target"
                            break
                        if d.close < stop:
                            exit_price, reason = d.close, "close below invalidation"
                            break
                    if exit_price is None:
                        exit_price = done[min(t + cfg.swing_max_hold_days, len(done) - 1)].close
                    trades["swing"].append(_trade(symbol, s, done[t].session_date, entry, exit_price * (1 - slip), entry - stop, reason, cfg))
    dates = sorted(all_dates)
    split = dates[int(len(dates) * (1 - cfg.out_of_sample_fraction))] if dates else None
    out = {}
    for horizon, items in trades.items():
        for item in items:
            item["sample"] = "out" if split and item["date"] >= split else "in"
        out[horizon] = {}
        for setup in ("developing_breakout", "momentum_continuation", "uptrend_pullback"):
            mine = [x for x in items if x["setup"] == setup]
            out[horizon][setup] = {"in_sample": _summary([x for x in mine if x["sample"] == "in"]), "out_of_sample": _summary([x for x in mine if x["sample"] == "out"]), "trades": mine[-50:]}
    return {"symbols": len(symbols), "split_date": split, "results": out, "notes": ["Setups are detected using only data available at each day's close.", "Overnight: entry at the signal day's close, exit at the stop, at a gap below it, or at the configured time next session.", "Swing: entry at the next open, stop at invalidation, target at the configured R, time stop after the holding limit; stop assumed before target within a day.", "Results describe this sample only and are not probabilities."]}


def _trade(symbol, setup, date, entry, exit_price, risk, reason, cfg) -> dict:
    # Per-share results; slippage is already in the entry and exit prices.
    pnl_per_share = exit_price - entry
    return {"symbol": symbol, "setup": setup["setup"], "date": date, "entry": entry, "exit": exit_price, "reason": reason, "r": pnl_per_share / risk if risk > 0 else None, "return_pct": pnl_per_share / entry * 100}


def _summary(items: list[dict]) -> dict:
    if not items:
        return {"trades": 0}
    rs = [x["r"] for x in items if x["r"] is not None]
    wins = [x for x in items if x["exit"] > x["entry"]]
    return {"trades": len(items), "win_rate": len(wins) / len(items), "avg_r": sum(rs) / len(rs) if rs else None, "avg_return_pct": sum(x["return_pct"] for x in items) / len(items), "worst_r": min(rs) if rs else None, "gap_exits": sum(1 for x in items if x["reason"].startswith("gap"))}

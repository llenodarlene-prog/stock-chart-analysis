"""FastAPI application. Analysis and alerts only: there is no order endpoint."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import analysis, backtest, plans, scanner, sessions
from .alpaca import AlpacaClient, AlpacaError
from .config import AnalysisConfig, BacktestConfig, load_settings
from .indicators import compute_all
from .store import Store
from .structure import detect_signals, swings

# Request logging would include query strings only; credentials travel in
# headers, which are never logged. Keep the HTTP client quiet regardless.
logging.getLogger("httpx").setLevel(logging.WARNING)

settings = load_settings()
store = Store(settings.database_path)
client = AlpacaClient(settings)
app = FastAPI(title="Stock chart analysis (analysis only, no orders)")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])

WINDOW_DAYS = {"1Min": 5, "5Min": 20, "15Min": 45, "1Hour": 90, "1Day": 400}
TAIL_OVERLAP = 3600  # re-fetch the last hour so late corrections replace cached bars
MIN_REFRESH = 20  # seconds between tail refreshes for the same symbol
FEED_LABELS = {"iex": "IEX (Investors Exchange only, about 2.5% of US volume)", "sip": "SIP (all US exchanges)"}
_last_error: dict[str, str] = {}


def _symbol(value: str) -> str:
    value = value.strip().upper()
    if not value or len(value) > 10 or not all(ch.isalnum() or ch in ".-" for ch in value):
        raise HTTPException(400, "Invalid symbol.")
    return value


def cache_key(adjustment: str) -> str:
    """Raw and adjusted bars are cached separately under different feed keys."""
    return settings.feed if adjustment == "raw" else f"{settings.feed}+{adjustment}"


def ensure_bars(symbol: str, start: int, now: int, adjustment: str = "raw") -> tuple[str | None, int]:
    """Fills the 1-minute cache for [start, now]. Returns (error message, corrections)."""
    key = cache_key(adjustment)
    cov = store.coverage(symbol, key)
    corrections = 0
    try:
        if cov is None:
            corrections += store.upsert_bars(symbol, key, client.bars_1min(symbol, start, now, adjustment))
            store.set_coverage(symbol, key, start, now)
        else:
            lo = cov["start"]
            if start < lo:
                corrections += store.upsert_bars(symbol, key, client.bars_1min(symbol, start, lo, adjustment))
                lo = start
            if time.time() - cov["fetched_at"] >= MIN_REFRESH:
                corrections += store.upsert_bars(symbol, key, client.bars_1min(symbol, max(lo, cov["end"] - TAIL_OVERLAP), now, adjustment))
                store.set_coverage(symbol, key, lo, now)
            elif lo != cov["start"]:
                store.set_coverage(symbol, key, lo, cov["end"])
        _last_error.pop(symbol, None)
        return None, corrections
    except AlpacaError as exc:
        _last_error[symbol] = str(exc)
        return str(exc), corrections


_calendar_loaded = {"at": 0.0, "status": "not loaded"}


def ensure_calendar(now: int) -> str:
    """Loads Alpaca's market calendar (holidays, early closes) twice a day."""
    if time.time() - _calendar_loaded["at"] < 12 * 3600 and sessions.CALENDAR:
        return _calendar_loaded["status"]
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    try:
        days = client.calendar(day(now - 800 * 86400), day(now + 30 * 86400))
        if days:
            store.save_calendar(days)
        _calendar_loaded["status"] = "Alpaca market calendar loaded"
    except AlpacaError as exc:
        _calendar_loaded["status"] = f"Market calendar unavailable ({exc}); using weekday hours, so holidays and early closes may show as missing candles."
    cached = list(store.calendar().values())
    sessions.load_calendar(cached)
    _calendar_loaded["at"] = time.time()
    return _calendar_loaded["status"]


def load_candles(symbol: str, timeframe: str, session_mode: str, days: int, now: int, adjustment: str, refresh: bool, until: int | None = None):
    """Candles for a timeframe from one cached source. Intraday timeframes use
    1-minute bars; daily candles use 30-minute bars (far less to download).
    Returns (candles, error, corrections, fetched_at)."""
    include = {"regular"} if session_mode == "regular" else {"pre", "regular", "post"}
    start = now - days * 86400
    cut = until if until else now
    if timeframe == "1Day":
        cfg = scanner.ScanConfig(lookback_days=days)
        errors = scanner.ensure_30m(client, store, [symbol], cfg, now, adjustment=adjustment, stale_after=MIN_REFRESH) if refresh else []
        key = cache_key(adjustment)
        bars = [b for b in store.bars_30m(symbol, key, start) if b.t <= cut]
        cov = store.coverage_30m(key).get(symbol)
        candles = sessions.aggregate(bars, "1Day", include, cut)
        return candles, (errors[0] if errors else None), 0, cov["fetched_at"] if cov else None
    error, corrections = ensure_bars(symbol, start, now, adjustment) if refresh else (None, 0)
    raw = store.bars(symbol, cache_key(adjustment), start, cut)
    cov = store.coverage(symbol, cache_key(adjustment))
    return sessions.aggregate(raw, timeframe, include, cut), error, corrections, cov["fetched_at"] if cov else None


STATS_DAYS = {"1Min": 20, "5Min": 60, "15Min": 120, "1Hour": 250, "1Day": 730}
_stats_cache: dict[tuple, tuple[float, dict]] = {}


def history_for(symbol: str, timeframe: str, session_mode: str, now: int, adjustment: str, refresh: bool) -> dict:
    """Past-signal outcomes over a longer history window, cached for 30 minutes."""
    key = (symbol, timeframe, session_mode, adjustment)
    hit = _stats_cache.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    cfg = AnalysisConfig.load()
    candles, *_ = load_candles(symbol, timeframe, session_mode, STATS_DAYS[timeframe], now, adjustment, refresh)
    completed = [c for c in candles if c.complete]
    ind = compute_all(completed, cfg, timeframe == "1Day")
    stats = analysis.history_stats(completed, ind, detect_signals(completed, ind, cfg), cfg)
    _stats_cache[key] = (time.time(), stats)
    return stats


def corporate_actions(symbol: str, start: int, now: int) -> tuple[list[dict], str | None]:
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    cached = store.cached_actions(symbol)
    if cached and cached["start"] <= day(start) and time.time() - cached["fetched_at"] < 12 * 3600:
        return cached["actions"], None
    try:
        actions = client.corporate_actions(symbol, day(start), day(now))
        store.save_actions(symbol, actions, day(start), day(now))
        return actions, None
    except AlpacaError as exc:
        return (cached or {}).get("actions", []), f"Corporate actions unavailable: {exc}"


def build(symbol: str, timeframe: str, session_mode: str, now: int | None = None, until: int | None = None, refresh: bool = True, adjustment: str = "raw") -> dict:
    if timeframe not in sessions.TIMEFRAMES:
        raise HTTPException(400, f"Timeframe must be one of {list(sessions.TIMEFRAMES)}.")
    if session_mode not in ("regular", "extended"):
        raise HTTPException(400, "sessions must be regular or extended.")
    if adjustment not in ("raw", "split", "all"):
        raise HTTPException(400, "adjustment must be raw, split or all.")
    now = now or int(time.time())
    calendar_status = ensure_calendar(now) if refresh else _calendar_loaded["status"]
    cfg = AnalysisConfig.load()
    start = now - WINDOW_DAYS[timeframe] * 86400
    include = {"regular"} if session_mode == "regular" else {"pre", "regular", "post"}
    candles, error, corrections, fetched_at = load_candles(symbol, timeframe, session_mode, WINDOW_DAYS[timeframe], now, adjustment, refresh, until)
    if until:
        # Replay: candles after the cut-off do not exist yet.
        candles = [c for c in candles if c.time <= until]
    daily = timeframe == "1Day"
    ind = compute_all(candles, cfg, daily)
    state = sessions.market_state(now)
    fetch_age = None if not fetched_at else (now - fetched_at) if not until else 0
    gates = analysis.quality_gates(candles, ind, cfg, timeframe, fetch_age, state != "closed" and not until, error or _last_error.get(symbol))
    completed = [c for c in candles if c.complete]
    signals = detect_signals(completed, ind, cfg)
    actions, action_error = corporate_actions(symbol, start, now) if refresh else ([], None)
    first, last = (candles[0].time, candles[-1].end) if candles else (start, now)
    affecting = [a for a in actions if a.get("type") in ("forward_splits", "reverse_splits", "unit_splits", "stock_dividends", "spin_offs", "cash_dividends", "stock_mergers", "cash_mergers", "stock_and_cash_mergers")]
    comparability = []
    for a in affecting:
        ex = a.get("ex_date") or a.get("process_date") or a.get("effective_date")
        if not ex or not (sessions.utc_iso(first)[:10] <= ex <= sessions.utc_iso(last)[:10]):
            continue
        splitlike = a["type"] in ("forward_splits", "reverse_splits", "unit_splits", "stock_dividends")
        if adjustment == "all" or (adjustment == "split" and splitlike):
            note = f"{a['type'].replace('_', ' ')} on {ex}: prices are adjusted for it."
        else:
            note = f"{a['type'].replace('_', ' ')} on {ex}: prices are raw (unadjusted), so candles before and after this date are not directly comparable. Switch Prices to 'Adjusted' to compare."
        comparability.append({"type": a["type"], "date": ex, "adjusted": note.endswith("adjusted for it."), "detail": {k: v for k, v in a.items() if k in ("old_rate", "new_rate", "rate", "cash", "special")}, "message": note})
    for s in signals:
        s["explanation"] = analysis.explain(s)
        s["confirmation_note"] = {"weak": "Early signal: weaker confirmation (no buffer, no volume check).", "candle": "Confirmed on a completed candle.", "retest": "Confirmed by a retest after a confirmed breakout.", "none": "Not confirmed."}[s["confirmation"]]
        recent = completed and s["index"] >= len(completed) - 2
        s["actionable"] = bool(recent and gates["actionable"] and s["confirmation"] != "none")
        s["blocked_reasons"] = gates["blocked_reasons"] if recent else ["Historical signal (replay only)."]
        s["comparability_caveat"] = any(c["date"] <= sessions.utc_iso(s["time"])[:10] and not c["adjusted"] for c in comparability)
        if recent and not until and refresh:
            store.record_signal(symbol, timeframe, session_mode, s, sessions.data_hash(candles))
    assessment = analysis.assess(candles, ind, cfg, signals, gates, timeframe)
    if assessment.get("next_steps"):
        if until:
            history = analysis.history_stats(completed, ind, signals, cfg)
        else:
            history = history_for(symbol, timeframe, session_mode, now, adjustment, refresh)
        others = [v[1] for k, v in _stats_cache.items() if k[1:] == (timeframe, session_mode, adjustment) and k[0] in set(store.watchlist()) | {symbol}]
        assessment["next_steps"]["scenarios"] = analysis.simple_scenarios(assessment["levels"], {k: dict(v) for k, v in history.items()}, timeframe, analysis.pool_stats(others) if not until else None)
        assessment["next_steps"]["history_window_days"] = STATS_DAYS[timeframe] if not until else None
    swing_points = [asdict(sw) for sw in swings(completed, cfg)]
    last_complete = completed[-1] if completed else None
    return {
        "symbol": symbol, "timeframe": timeframe, "sessions": session_mode,
        "feed": settings.feed, "feed_label": FEED_LABELS.get(settings.feed, settings.feed), "adjustment": adjustment,
        "volume_note": "Volume counts trades on the selected feed only." if settings.feed == "iex" else "",
        "provenance": {
            "now": now, "replay_until": until, "fetched_at": fetched_at,
            "fetch_age_seconds": fetch_age, "market_state": state, "calendar": calendar_status,
            "source": "30-minute bars, regular session" if daily else "1-minute bars",
            "last_candle_time": candles[-1].time if candles else None, "last_completed_time": last_complete.time if last_complete else None,
            "last_completed_end": last_complete.end if last_complete else None,
            "freshness_seconds": now - last_complete.end if last_complete else None,
            "rate_limit": client.last_rate_limit, "late_corrections_this_fetch": corrections,
            "recent_corrections": store.revisions(symbol, cache_key(adjustment), first),
            "session_rules": "America/New_York. Pre-market, regular and after-hours times come from Alpaca's market calendar (holidays and early closes included) when it loads; otherwise 04:00-09:30, 09:30-16:00 and 16:00-20:00 on weekdays.",
        },
        "error": error, "corporate_action_error": action_error,
        "missing": {"expected_buckets": sessions.expected_buckets(candles, timeframe, include), "present": len(candles)},
        "candles": [c.to_dict() for c in candles], "indicators": ind, "swings": swing_points,
        "signals": signals, "assessment": assessment, "gates": gates,
        "corporate_actions": comparability, "data_hash": sessions.data_hash(candles), "config": cfg.to_dict(),
    }


@app.get("/api/health")
def health() -> dict:
    return {"credentials_configured": settings.has_credentials, "feed": settings.feed, "orders": "not implemented by design"}


@app.get("/api/candles")
def candles(symbol: str, timeframe: str = "5Min", sessions_mode: str = Query("regular", alias="sessions"), adjustment: str = "raw") -> dict:
    return build(_symbol(symbol), timeframe, sessions_mode, adjustment=adjustment)


@app.get("/api/replay")
def replay(symbol: str, until: int, timeframe: str = "5Min", sessions_mode: str = Query("regular", alias="sessions"), adjustment: str = "raw") -> dict:
    """Everything recomputed from candles up to `until`, as a live system would have seen it."""
    return build(_symbol(symbol), timeframe, sessions_mode, until=until, refresh=False, adjustment=adjustment)


@app.post("/api/backtest")
def run_backtest(payload: dict = Body(...)) -> dict:
    symbol = _symbol(payload.get("symbol", ""))
    data = build(symbol, payload.get("timeframe", "5Min"), payload.get("sessions", "regular"), adjustment=payload.get("adjustment", "raw"))
    cfg = AnalysisConfig.load()
    candle_objs = [sessions.Candle(**c) for c in data["candles"]]
    known = set(BacktestConfig.__dataclass_fields__)
    results = {}
    for variant in payload.get("variants") or ["early", "confirmed", "retest"]:
        bcfg = replace(BacktestConfig(), **{k: v for k, v in (payload.get("config") or {}).items() if k in known}, entry_variant=variant)
        results[variant] = backtest.run(candle_objs, data["indicators"], data["signals"], cfg, bcfg)
    return {"symbol": symbol, "timeframe": data["timeframe"], "sessions": data["sessions"], "feed": data["feed"], "data_hash": data["data_hash"], "results": results}


@app.get("/api/symbols")
def symbols(q: str = "") -> dict:
    q = q.strip().upper()
    try:
        assets = client.assets()
    except AlpacaError as exc:
        raise HTTPException(502, str(exc))
    matches = [a for a in assets if a["symbol"].startswith(q) or q in a["name"].upper()] if q else []
    matches.sort(key=lambda a: (a["symbol"] != q, not a["symbol"].startswith(q), a["symbol"]))
    return {"results": matches[:20]}


@app.get("/api/watchlist")
def get_watchlist() -> dict:
    return {"symbols": store.watchlist()}


@app.post("/api/watchlist/{symbol}")
def add_watch(symbol: str) -> dict:
    store.add_watch(_symbol(symbol))
    return {"symbols": store.watchlist()}


@app.delete("/api/watchlist/{symbol}")
def remove_watch(symbol: str) -> dict:
    store.remove_watch(_symbol(symbol))
    return {"symbols": store.watchlist()}


@app.post("/api/plans/calculate")
def calculate_plan(payload: dict = Body(...)) -> dict:
    try:
        return plans.calculate(payload, payload.get("algo_levels"))
    except plans.PlanError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/plans")
def list_plans() -> dict:
    items = store.plans()
    for p in items:
        p["outcome"] = plans.outcome(p, p.get("actual_exit"), p.get("actual_shares"))
    return {"plans": items}


@app.post("/api/plans")
def save_plan(payload: dict = Body(...)) -> dict:
    plan_id = payload.pop("id", None)
    existing = next((p for p in store.plans() if p["id"] == plan_id), None) if plan_id else None
    if existing:
        payload["original_stop"] = existing.get("stop")  # the last saved stop; it may only tighten
    else:
        payload["original_stop"] = payload.get("stop")
    try:
        plans.calculate(payload, payload.get("algo_levels"))
    except plans.PlanError as exc:
        raise HTTPException(400, str(exc))
    return {"id": store.save_plan(payload, plan_id)}


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int) -> dict:
    store.delete_plan(plan_id)
    return {"deleted": plan_id}


@app.get("/api/signals/history")
def signal_history(symbol: str, timeframe: str = "5Min", sessions_mode: str = Query("regular", alias="sessions")) -> dict:
    return {"signals": store.signal_history(_symbol(symbol), timeframe, sessions_mode)}


# Next Trading Day Watchlist scanner ---------------------------------------------------


@app.post("/api/universe/refresh")
def refresh_universe() -> dict:
    try:
        return scanner.refresh_universe(store)
    except (AlpacaError, Exception) as exc:  # network errors from the directory fetch
        raise HTTPException(502, f"Could not refresh the GoTrade directory: {exc}")


@app.get("/api/universe")
def get_universe() -> dict:
    rows = store.universe()
    availability = store.availability()
    head = rows[0] if rows else {}
    return {"count": len(rows), "source_url": head.get("source_url"), "source_last_modified": head.get("source_last_modified"), "fetched_at": head.get("fetched_at"),
            "confirmed_available": sum(1 for a in availability.values() if a["available"]), "confirmed_unavailable": sum(1 for a in availability.values() if not a["available"]),
            "note": "Universe is the official GoTrade directory. Whether a symbol is open to your account is confirmed by you, not inferred from Alpaca."}


@app.post("/api/availability/{symbol}")
def set_availability(symbol: str, payload: dict = Body(...)) -> dict:
    store.set_availability(_symbol(symbol), bool(payload.get("available")), str(payload.get("note", ""))[:200])
    return store.availability()[_symbol(symbol)]


_scan_job: dict = {"state": "idle"}
_scan_lock = threading.Lock()


def _scan_worker(cfg, symbols) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _scan_job.update(phase=phase, done=done, total=total)
    try:
        result = scanner.run_scan(client, store, AnalysisConfig.load(), cfg, int(time.time()), symbols, progress)
        result["id"] = store.save_scan(result)
        store.update_scan(result["id"], result)
        _scan_job.update(state="done", scan_id=result["id"], phase="Finished", finished_at=time.time())
    except Exception as exc:  # report every failure on screen
        _scan_job.update(state="error", error=str(exc), finished_at=time.time())


@app.post("/api/scanner/run")
def run_scanner(payload: dict = Body(default={})) -> dict:
    """Starts a scan in the background. Poll /api/scanner/job for progress."""
    cfg = replace(scanner.ScanConfig(), **{k: v for k, v in (payload.get("config") or {}).items() if k in scanner.ScanConfig.__dataclass_fields__})
    symbols = [_symbol(s) for s in payload.get("symbols") or []] or None
    if not store.universe():
        raise HTTPException(400, "The GoTrade universe has not been loaded. Refresh it first.")
    with _scan_lock:
        if _scan_job.get("state") == "running":
            return dict(_scan_job)
        _scan_job.clear()
        _scan_job.update(state="running", started_at=time.time(), phase="Starting", done=0, total=len(symbols or store.universe()))
        threading.Thread(target=_scan_worker, args=(cfg, symbols), daemon=True).start()
    return dict(_scan_job)


@app.get("/api/scanner/job")
def scan_job() -> dict:
    return dict(_scan_job)


@app.get("/api/scanner/latest")
def latest_scan() -> dict:
    return store.scan() or {"candidates": [], "message": "No scan has been run yet."}


@app.post("/api/scanner/{scan_id}/reassess")
def reassess_scan(scan_id: int) -> dict:
    scan = store.scan(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found.")
    try:
        updated = scanner.reassess(scan, client, int(time.time()))
    except AlpacaError as exc:
        raise HTTPException(502, str(exc))
    store.update_scan(scan_id, {k: v for k, v in updated.items() if k not in ("id",)})
    return updated


@app.get("/api/scanner/chart/{symbol}")
def scanner_chart(symbol: str) -> dict:
    """Daily candles built from the cached 30-minute bars the scan used."""
    symbol = _symbol(symbol)
    now = int(time.time())
    cfg = scanner.ScanConfig()
    done = [c for c in scanner.daily_candles(store.bars_30m(symbol, settings.feed, now - cfg.lookback_days * 86400), now)]
    ind = compute_all(done, AnalysisConfig.load(), daily=True)
    return {"symbol": symbol, "feed": settings.feed, "candles": [c.to_dict() for c in done], "indicators": {k: ind[k] for k in ("ema_fast", "ema_slow")}, "data_hash": sessions.data_hash(done)}


@app.post("/api/scanner/backtest")
def scanner_backtest(payload: dict = Body(default={})) -> dict:
    cfg = replace(scanner.ScanConfig(), **{k: v for k, v in (payload.get("config") or {}).items() if k in scanner.ScanConfig.__dataclass_fields__})
    universe = [u["symbol"] for u in store.universe()]
    symbols = [_symbol(s) for s in payload.get("symbols") or []]
    if not symbols:
        # A deterministic spread across the whole universe, not a hand-picked list.
        step = max(1, len(universe) // int(payload.get("sample_size", 150)))
        symbols = universe[::step]
    now = int(time.time())
    errors = scanner.ensure_30m(client, store, symbols, cfg, now)
    result = scanner.backtest(store, settings.feed, symbols, AnalysisConfig.load(), cfg, now)
    result["errors"] = errors
    result["sampled_symbols"] = symbols[:200]
    return result


# Background watchlist monitor and alerts ------------------------------------------------
MONITOR_FILE = settings.database_path.parent / "monitor.json" if str(settings.database_path) != ":memory:" else None
MONITOR_DEFAULTS = {"enabled": True, "timeframe": "5Min", "sessions": "regular", "adjustment": "raw", "interval_seconds": 60}
_monitor_state: dict = {"running": False, "last_run": None, "last_error": None, "checked": []}


def monitor_settings() -> dict:
    if MONITOR_FILE and MONITOR_FILE.exists():
        try:
            return {**MONITOR_DEFAULTS, **json.loads(MONITOR_FILE.read_text())}
        except ValueError:
            pass
    return dict(MONITOR_DEFAULTS)


ALERT_KINDS = {
    ("breakout", "confirmed"): ("Breakout confirmed", "closed above the breakout level with volume"),
    ("breakout", "early"): ("Early breakout", "crossed the level (weaker confirmation)"),
    ("retest", "retest"): ("Retest held", "returned to the breakout level and held"),
    ("failed_breakout", "failed"): ("Breakout failed", "fell back through the breakout level"),
}


def alerts_from(data: dict) -> int:
    """Creates alerts for newly confirmed events on the latest completed candles."""
    symbol, tf = data["symbol"], data["timeframe"]
    made = 0
    for s in data["signals"]:
        if s.get("blocked_reasons") == ["Historical signal (replay only)."]:
            continue  # only events on the latest completed candles
        info = ALERT_KINDS.get((s["kind"], s["variant"]))
        if not info:
            continue
        direction = "up" if s["direction"] == "bullish" else "down"
        title = f"{symbol}: {info[0]} ({direction}, {tf})"
        msg = f"{symbol} {info[1]} at ${s['level']:,.2f}. {s['explanation']}"
        if not s["actionable"]:
            msg += " Data checks are not passing, so treat this as information only."
        made += store.add_alert(symbol, tf, f"{s['kind']}:{s['variant']}:{s['direction']}", s["time"], s["level"], s["actionable"], title, msg)
    a = data["assessment"]
    if a.get("as_of"):
        for e in a["evidence"]:
            if e["category"] == "invalidation":
                made += store.add_alert(symbol, tf, f"invalidation:{e['direction']}", a["as_of"], None, data["gates"]["actionable"], f"{symbol}: structure broken ({tf})", e["text"])
        close = a["levels"]["close"]
        for plan in store.plans():
            if plan.get("symbol") != symbol or plan.get("actual_exit"):
                continue
            sign = 1 if plan.get("side", "long") == "long" else -1
            try:
                stop = float(plan["stop"])
            except (KeyError, TypeError, ValueError):
                continue
            if sign * (close - stop) <= 0:
                made += store.add_alert(symbol, tf, f"plan{plan['id']}:stop", 0, stop, True, f"{symbol}: your plan's stop level was reached", f"A completed {tf} candle closed at ${close:,.2f}, beyond your stop ${stop:,.2f} (plan #{plan['id']}). Your exit decision.")
            for i, t in enumerate(plan.get("targets") or [], 1):
                if t.get("price") and sign * (close - float(t["price"])) >= 0:
                    made += store.add_alert(symbol, tf, f"plan{plan['id']}:target{i}", 0, t["price"], True, f"{symbol}: your target {i} was reached", f"A completed {tf} candle closed at ${close:,.2f}, at or beyond your target ${float(t['price']):,.2f} (plan #{plan['id']}).")
    return made


def monitor_once() -> None:
    cfg = monitor_settings()
    checked = []
    for symbol in store.watchlist():
        try:
            data = build(symbol, cfg["timeframe"], cfg["sessions"], adjustment=cfg["adjustment"])
            new = alerts_from(data)
            checked.append({"symbol": symbol, "ok": data["error"] is None, "new_alerts": new, "label": data["assessment"]["label"], "error": data["error"]})
        except Exception as exc:  # keep checking the other symbols
            checked.append({"symbol": symbol, "ok": False, "error": str(exc)})
    _monitor_state.update(last_run=time.time(), checked=checked, last_error=None)


def _monitor_loop() -> None:
    _monitor_state["running"] = True
    while True:
        cfg = monitor_settings()
        if cfg["enabled"] and settings.has_credentials:
            try:
                monitor_once()
            except Exception as exc:
                _monitor_state["last_error"] = str(exc)
        time.sleep(max(30, int(cfg["interval_seconds"])))


@app.on_event("startup")
def start_monitor() -> None:
    import os
    if os.environ.get("MONITOR", "1") != "0" and settings.has_credentials:
        threading.Thread(target=_monitor_loop, daemon=True).start()


@app.get("/api/monitor")
def monitor_status() -> dict:
    return {"settings": monitor_settings(), **_monitor_state, "watchlist": store.watchlist()}


@app.post("/api/monitor/settings")
def set_monitor(payload: dict = Body(...)) -> dict:
    cfg = monitor_settings()
    if payload.get("timeframe") in sessions.TIMEFRAMES:
        cfg["timeframe"] = payload["timeframe"]
    if payload.get("sessions") in ("regular", "extended"):
        cfg["sessions"] = payload["sessions"]
    if payload.get("adjustment") in ("raw", "split", "all"):
        cfg["adjustment"] = payload["adjustment"]
    if "enabled" in payload:
        cfg["enabled"] = bool(payload["enabled"])
    if MONITOR_FILE:
        MONITOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        MONITOR_FILE.write_text(json.dumps(cfg))
    return cfg


@app.post("/api/monitor/run")
def run_monitor_now() -> dict:
    monitor_once()
    return monitor_status()


@app.get("/api/alerts")
def get_alerts(since: int = 0) -> dict:
    items = store.alerts(since)
    return {"alerts": items, "unread": sum(1 for a in items if not a["read"])}


@app.post("/api/alerts/read")
def read_alerts() -> dict:
    store.mark_alerts_read()
    return {"ok": True}

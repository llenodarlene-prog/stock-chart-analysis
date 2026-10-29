"""FastAPI application. Analysis and alerts only: there is no order endpoint."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import analysis, backtest, plans, sessions
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


def ensure_bars(symbol: str, start: int, now: int) -> tuple[str | None, int]:
    """Fills the cache for [start, now]. Returns (error message, corrections)."""
    cov = store.coverage(symbol, settings.feed)
    corrections = 0
    try:
        if cov is None:
            corrections += store.upsert_bars(symbol, settings.feed, client.bars_1min(symbol, start, now))
            store.set_coverage(symbol, settings.feed, start, now)
        else:
            lo = cov["start"]
            if start < lo:
                corrections += store.upsert_bars(symbol, settings.feed, client.bars_1min(symbol, start, lo))
                lo = start
            if time.time() - cov["fetched_at"] >= MIN_REFRESH:
                corrections += store.upsert_bars(symbol, settings.feed, client.bars_1min(symbol, max(lo, cov["end"] - TAIL_OVERLAP), now))
                store.set_coverage(symbol, settings.feed, lo, now)
            elif lo != cov["start"]:
                store.set_coverage(symbol, settings.feed, lo, cov["end"])
        _last_error.pop(symbol, None)
        return None, corrections
    except AlpacaError as exc:
        _last_error[symbol] = str(exc)
        return str(exc), corrections


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


def build(symbol: str, timeframe: str, session_mode: str, now: int | None = None, until: int | None = None, refresh: bool = True) -> dict:
    if timeframe not in sessions.TIMEFRAMES:
        raise HTTPException(400, f"Timeframe must be one of {list(sessions.TIMEFRAMES)}.")
    if session_mode not in ("regular", "extended"):
        raise HTTPException(400, "sessions must be regular or extended.")
    now = now or int(time.time())
    cfg = AnalysisConfig.load()
    start = now - WINDOW_DAYS[timeframe] * 86400
    error, corrections = ensure_bars(symbol, start, now) if refresh else (None, 0)
    include = {"regular"} if session_mode == "regular" else {"pre", "regular", "post"}
    raw = store.bars(symbol, settings.feed, start, until if until else now)
    candles = sessions.aggregate(raw, timeframe, include, until if until else now)
    if until:
        # Replay: candles after the cut-off do not exist yet.
        candles = [c for c in candles if c.time <= until]
    daily = timeframe == "1Day"
    ind = compute_all(candles, cfg, daily)
    cov = store.coverage(symbol, settings.feed)
    state = sessions.market_state(now)
    fetch_age = None if not cov else (now - cov["fetched_at"]) if not until else 0
    gates = analysis.quality_gates(candles, ind, cfg, timeframe, fetch_age, state != "closed" and not until, error or _last_error.get(symbol))
    completed = [c for c in candles if c.complete]
    signals = detect_signals(completed, ind, cfg)
    actions, action_error = corporate_actions(symbol, start, now) if refresh else ([], None)
    first, last = (candles[0].time, candles[-1].end) if candles else (start, now)
    affecting = [a for a in actions if a.get("type") in ("forward_splits", "reverse_splits", "unit_splits", "stock_dividends", "spin_offs", "cash_dividends", "stock_mergers", "cash_mergers", "stock_and_cash_mergers")]
    comparability = []
    for a in affecting:
        ex = a.get("ex_date") or a.get("process_date") or a.get("effective_date")
        if ex and sessions.utc_iso(first)[:10] <= ex <= sessions.utc_iso(last)[:10]:
            comparability.append({"type": a["type"], "date": ex, "detail": {k: v for k, v in a.items() if k in ("old_rate", "new_rate", "rate", "cash", "special")}, "message": f"{a['type'].replace('_', ' ')} on {ex}: prices are raw (unadjusted), so candles before and after this date are not directly comparable."})
    tf_seconds = (sessions.TIMEFRAMES[timeframe] or 1440) * 60
    for s in signals:
        s["explanation"] = analysis.explain(s)
        s["confirmation_note"] = {"weak": "Early signal: weaker confirmation (no buffer, no volume check).", "candle": "Confirmed on a completed candle.", "retest": "Confirmed by a retest after a confirmed breakout.", "none": "Not confirmed."}[s["confirmation"]]
        recent = completed and s["index"] >= len(completed) - 2
        s["actionable"] = bool(recent and gates["actionable"] and s["confirmation"] != "none")
        s["blocked_reasons"] = gates["blocked_reasons"] if recent else ["Historical signal (replay only)."]
        s["comparability_caveat"] = any(c["date"] <= sessions.utc_iso(s["time"])[:10] for c in comparability)
        if recent and not until and refresh:
            store.record_signal(symbol, timeframe, session_mode, s, sessions.data_hash(candles))
    assessment = analysis.assess(candles, ind, cfg, signals, gates)
    swing_points = [asdict(sw) for sw in swings(completed, cfg)]
    last_complete = completed[-1] if completed else None
    return {
        "symbol": symbol, "timeframe": timeframe, "sessions": session_mode,
        "feed": settings.feed, "feed_label": FEED_LABELS.get(settings.feed, settings.feed), "adjustment": "raw",
        "volume_note": "Volume counts trades on the selected feed only." if settings.feed == "iex" else "",
        "provenance": {
            "now": now, "replay_until": until, "fetched_at": cov["fetched_at"] if cov else None,
            "fetch_age_seconds": fetch_age, "market_state": state,
            "last_candle_time": candles[-1].time if candles else None, "last_completed_time": last_complete.time if last_complete else None,
            "last_completed_end": last_complete.end if last_complete else None,
            "freshness_seconds": now - last_complete.end if last_complete else None,
            "rate_limit": client.last_rate_limit, "late_corrections_this_fetch": corrections,
            "recent_corrections": store.revisions(symbol, settings.feed, first),
            "session_rules": "America/New_York. Pre 04:00-09:30, regular 09:30-16:00, after-hours 16:00-20:00. Holidays and early closes are not modeled; they appear as missing candles.",
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
def candles(symbol: str, timeframe: str = "5Min", sessions_mode: str = Query("regular", alias="sessions")) -> dict:
    return build(_symbol(symbol), timeframe, sessions_mode)


@app.get("/api/replay")
def replay(symbol: str, until: int, timeframe: str = "5Min", sessions_mode: str = Query("regular", alias="sessions")) -> dict:
    """Everything recomputed from candles up to `until`, as a live system would have seen it."""
    return build(_symbol(symbol), timeframe, sessions_mode, until=until, refresh=False)


@app.post("/api/backtest")
def run_backtest(payload: dict = Body(...)) -> dict:
    symbol = _symbol(payload.get("symbol", ""))
    data = build(symbol, payload.get("timeframe", "5Min"), payload.get("sessions", "regular"))
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

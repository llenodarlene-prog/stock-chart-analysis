"""Session boundaries, candle aggregation, and candle completion.

All timeframes are built from the same cached raw 1-minute bars, so the chart
and every calculation read one candle list. Session boundaries use
America/New_York: pre-market 04:00-09:30, regular 09:30-16:00, after-hours
16:00-20:00. Buckets are anchored at each session's start, so a regular-session
hourly candle runs 09:30-10:30 and never mixes pre-market trades.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from .alpaca import Bar

NY = ZoneInfo("America/New_York")
MANILA = ZoneInfo("Asia/Manila")
SESSIONS = (("pre", dtime(4, 0), dtime(9, 30)), ("regular", dtime(9, 30), dtime(16, 0)), ("post", dtime(16, 0), dtime(20, 0)))
TIMEFRAMES = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60, "1Day": None}
# Grace after a bucket closes before it counts as complete, so a final
# minute bar that arrives late is still included.
COMPLETION_GRACE_SECONDS = 60


@dataclass
class Candle:
    time: int  # bucket start, UNIX seconds UTC
    end: int  # bucket end (exclusive)
    open: float
    high: float
    low: float
    close: float
    volume: int
    session: str
    session_date: str  # New York calendar date
    slot: int  # bucket index within its session (for relative volume)
    minutes: int  # raw 1-minute bars inside the bucket
    complete: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# Trading days from Alpaca's market calendar: date -> list of (session, start, end).
# Days inside CALENDAR_RANGE but missing from CALENDAR are market holidays.
# Without a calendar, weekday clock times are used.
CALENDAR: dict[str, list[tuple[str, dtime, dtime]]] = {}
CALENDAR_RANGE: tuple[str, str] | None = None


def _t(text: str) -> dtime:
    text = text.replace(":", "")
    return dtime(int(text[:2]), int(text[2:4]))


def load_calendar(days: list[dict]) -> None:
    """Installs calendar rows (date, open, close, session_open, session_close)."""
    global CALENDAR_RANGE
    CALENDAR.clear()
    for d in days:
        try:
            pre, op, cl, post = _t(d.get("session_open") or "0400"), _t(d["open"]), _t(d["close"]), _t(d.get("session_close") or "2000")
        except (KeyError, ValueError, TypeError):
            continue
        CALENDAR[d["date"]] = [("pre", pre, op), ("regular", op, cl), ("post", cl, post)]
    CALENDAR_RANGE = (min(CALENDAR), max(CALENDAR)) if CALENDAR else None


def day_sessions(day) -> list[tuple[str, dtime, dtime]]:
    key = day.isoformat()
    if key in CALENDAR:
        return CALENDAR[key]
    if CALENDAR_RANGE and CALENDAR_RANGE[0] <= key <= CALENDAR_RANGE[1]:
        return []  # market holiday
    return [] if day.weekday() >= 5 else list(SESSIONS)


def session_of(ts: int) -> tuple[str, datetime, datetime] | None:
    local = datetime.fromtimestamp(ts, NY)
    day = local.date()
    for name, start, end in day_sessions(day):
        if start <= local.time() < end:
            return name, datetime.combine(day, start, NY), datetime.combine(day, end, NY)
    return None


def aggregate(bars: list[Bar], timeframe: str, include: set[str], now: int) -> list[Candle]:
    """Groups raw 1-minute bars into candles for the chosen sessions."""
    size = TIMEFRAMES[timeframe]
    groups: dict[tuple, list[Bar]] = {}
    meta: dict[tuple, tuple] = {}
    for bar in bars:
        found = session_of(bar.t)
        if not found:
            continue
        name, start, end = found
        if name not in include:
            continue
        if size is None:
            # Daily candles: one per New York date across the included sessions.
            day = start.date()
            todays = [x for x in day_sessions(day) if x[0] in include]
            first = min(datetime.combine(day, s, NY) for _, s, _ in todays)
            last = max(datetime.combine(day, e, NY) for _, _, e in todays)
            key = (day.isoformat(),)
            meta[key] = (int(first.timestamp()), int(last.timestamp()), "regular" if include == {"regular"} else "extended", day.isoformat(), 0)
        else:
            offset = int((bar.t - start.timestamp()) // 60)
            slot = offset // size
            bucket_start = start + timedelta(minutes=slot * size)
            bucket_end = min(bucket_start + timedelta(minutes=size), end)
            key = (int(bucket_start.timestamp()),)
            meta[key] = (int(bucket_start.timestamp()), int(bucket_end.timestamp()), name, start.date().isoformat(), slot)
        groups.setdefault(key, []).append(bar)
    candles = []
    for key in sorted(groups, key=lambda k: meta[k][0]):
        items = groups[key]
        begin, finish, session, day, slot = meta[key]
        candles.append(Candle(begin, finish, items[0].o, max(b.h for b in items), min(b.l for b in items), items[-1].c, sum(b.v for b in items), session, day, slot, len(items), now >= finish + COMPLETION_GRACE_SECONDS))
    return candles


def data_hash(candles: list[Candle]) -> str:
    """SHA-256 of the canonical candle list the chart and calculations share."""
    canonical = [[c.time, f"{c.open:.6f}", f"{c.high:.6f}", f"{c.low:.6f}", f"{c.close:.6f}", c.volume, c.complete] for c in candles]
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def market_state(now: int) -> str:
    """Which session is in progress now, using the market calendar when loaded."""
    found = session_of(now)
    return found[0] if found else "closed"


def trading_dates(candles: list[Candle]) -> list[str]:
    """Trading dates spanned by the candles: from the calendar when it covers
    them (so a day with no trades still counts), else the dates present."""
    present = sorted({c.session_date for c in candles})
    if not present:
        return []
    if CALENDAR_RANGE and CALENDAR_RANGE[0] <= present[0] and present[-1] <= CALENDAR_RANGE[1]:
        return sorted(d for d in CALENDAR if present[0] <= d <= present[-1])
    return present


def expected_buckets(candles: list[Candle], timeframe: str, include: set[str]) -> int:
    """Buckets that should exist on the trading dates spanned by the data,
    including early closes."""
    from datetime import date

    size = TIMEFRAMES[timeframe]
    dates = trading_dates(candles)
    if size is None:
        return len(dates)
    total = 0
    for d in dates:
        for name, start, end in day_sessions(date.fromisoformat(d)):
            if name in include:
                minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
                total += -(-minutes // size)
    return total


def fmt(ts: int, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%d %H:%M %Z")


def utc_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

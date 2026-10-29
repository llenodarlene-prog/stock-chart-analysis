"""Builds raw 1-minute bars for tests. Test fixtures only; the app never
generates or substitutes market data."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.alpaca import Bar

NY = ZoneInfo("America/New_York")


def ny(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def minute_bars(start_ts, closes, volume=100):
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        bars.append(Bar(start_ts + 60 * i, o, max(o, c) + 0.01, min(o, c) - 0.01, c, volume, 1, None))
        prev = c
    return bars

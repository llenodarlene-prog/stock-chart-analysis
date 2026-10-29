"""Alpaca Market Data client.

Follows the Market Data API reference: GET /v2/stocks/bars with
APCA-API-KEY-ID / APCA-API-SECRET-KEY headers, `next_page_token` pagination
(limit up to 10,000), 429 responses with X-RateLimit-Reset, and
GET /v1/corporate-actions. The feed is always passed explicitly and never
replaced with another feed or with generated data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .config import Settings


class AlpacaError(Exception):
    """An API failure with a message that is safe to show (no headers or keys)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Bar:
    t: int  # bar start, UNIX seconds UTC
    o: float
    h: float
    l: float
    c: float
    v: int
    n: int | None
    vw: float | None


def _iso_to_epoch(text: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


class AlpacaClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.settings = settings
        self._sleep = sleep
        self._client = httpx.Client(timeout=20.0, transport=transport)
        self.last_rate_limit: dict | None = None

    def _headers(self) -> dict:
        if not self.settings.has_credentials:
            raise AlpacaError("Alpaca credentials are missing. Set ALPACA_API_KEY and ALPACA_API_SECRET in backend/.env.")
        return {"APCA-API-KEY-ID": self.settings.api_key, "APCA-API-SECRET-KEY": self.settings.api_secret}

    def _get(self, url: str, params: dict) -> dict:
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._client.get(url, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                if attempts >= 4:
                    raise AlpacaError(f"Network error reaching Alpaca after {attempts} attempts: {type(exc).__name__}") from None
                self._sleep(2 ** attempts)
                continue
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                self.last_rate_limit = {"limit": response.headers.get("X-RateLimit-Limit"), "remaining": remaining, "reset": response.headers.get("X-RateLimit-Reset")}
            if response.status_code == 429 and attempts < 5:
                reset = response.headers.get("X-RateLimit-Reset")
                wait = max(1.0, float(reset) - time.time()) if reset else 2 ** attempts
                self._sleep(min(wait, 60.0))
                continue
            if response.status_code >= 500 and attempts < 4:
                self._sleep(2 ** attempts)
                continue
            if response.status_code != 200:
                try:
                    detail = response.json().get("message", "")
                except ValueError:
                    detail = response.text[:200]
                hint = {401: "Authentication failed. Check the key and secret.", 403: "Access forbidden. The feed may need a subscription.", 429: "Rate limit still exceeded after retries."}.get(response.status_code, "")
                raise AlpacaError(f"Alpaca returned HTTP {response.status_code}. {hint} {detail}".strip(), response.status_code)
            return response.json()

    def bars_1min(self, symbol: str, start: int, end: int) -> list[Bar]:
        """All raw 1-minute bars for [start, end], following every page."""
        from datetime import datetime, timezone

        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"symbols": symbol, "timeframe": "1Min", "start": iso(start), "end": iso(end), "limit": 10000, "adjustment": "raw", "feed": self.settings.feed, "sort": "asc"}
        bars: list[Bar] = []
        while True:
            data = self._get(f"{self.settings.data_host}/v2/stocks/bars", params)
            for raw in (data.get("bars") or {}).get(symbol, []) or []:
                bars.append(Bar(_iso_to_epoch(raw["t"]), float(raw["o"]), float(raw["h"]), float(raw["l"]), float(raw["c"]), int(raw["v"]), raw.get("n"), raw.get("vw")))
            token = data.get("next_page_token")
            if not token:
                return bars
            params = {**params, "page_token": token}

    def corporate_actions(self, symbol: str, start_date: str, end_date: str) -> list[dict]:
        params = {"symbols": symbol, "start": start_date, "end": end_date, "limit": 1000}
        actions: list[dict] = []
        while True:
            data = self._get(f"{self.settings.data_host}/v1/corporate-actions", params)
            for kind, items in (data.get("corporate_actions") or {}).items():
                for item in items or []:
                    actions.append({"type": kind, **item})
            token = data.get("next_page_token")
            if not token:
                return actions
            params = {**params, "page_token": token}

    def assets(self) -> list[dict]:
        data = self._get(f"{self.settings.trading_host}/v2/assets", {"status": "active", "asset_class": "us_equity"})
        return [{"symbol": a["symbol"], "name": a.get("name", ""), "exchange": a.get("exchange", ""), "tradable": a.get("tradable", False)} for a in data]

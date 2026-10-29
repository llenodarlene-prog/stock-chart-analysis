# Personal US Stock Chart Analysis

A local web app for chart analysis and alerts on US stocks: candlestick charts, rule-based signals, trade-plan calculations, replay, and backtesting.

**This app does not place orders.** Every decision is yours. Nothing in it is a forecast, a probability, or financial advice.

- Frontend: React and TypeScript, built with Vite, using TradingView Lightweight Charts 5.2.1
- Backend: Python FastAPI
- Data: Alpaca Market Data API, on the IEX feed by default
- Storage: SQLite, for cached 1-minute bars, signal history, the watchlist, and plans

## Local Setup

You need Python 3.11 or later and Node 20 or later.

```bash
# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then add ALPACA_API_KEY and ALPACA_API_SECRET
.venv/bin/uvicorn app.main:app --port 8000

# Frontend (second terminal)
cd frontend
npm install
npm run dev                 # open http://localhost:5173
```

Run the tests:

```bash
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm test && npm run build
```

### Credentials

- `ALPACA_API_KEY` and `ALPACA_API_SECRET` are read only by the backend, from the environment or from `backend/.env`.
- `.env` is in `.gitignore`.
- The browser talks only to the local backend through the Vite proxy. Keys are sent to Alpaca in request headers, and they are never returned by the API, logged, or written to the database.
- If the keys are missing, the app shows an explicit error and loads no data.

## Market Data Rules

- **Feed.** `iex` is the default and the only feed that works without a paid subscription. IEX carries roughly 2.5% of US volume, so volume and some prints differ from consolidated (SIP) data. The feed is labeled on every screen. The app never switches to another feed or to generated data. A failed request shows the error. Previously cached candles are kept and labeled as cached.
- **One data source.** The backend caches raw 1-minute bars with `adjustment=raw`. It builds every timeframe from that cache: 1-minute, 5-minute, 15-minute, hourly and daily. The chart, the indicators, the signals, and the backtest all read the same candle list. The response carries a SHA-256 hash of that list. The browser recomputes the hash from the candles it draws and shows whether the two match.
- **Sessions.** Boundaries use America/New_York:

  | Session | Hours (ET) |
  |---|---|
  | Pre-market | 04:00 to 09:30 |
  | Regular | 09:30 to 16:00 |
  | After-hours | 16:00 to 20:00 |

  "Regular only" excludes extended trading entirely. Candles are anchored at each session's start, so a regular hourly candle runs 09:30 to 10:30 and never mixes pre-market trades. Times can be displayed in New York or Manila time.
- **Forming candles.** A candle is complete 60 seconds after its bucket ends. Daily candles are complete after the last included session closes. Forming candles are drawn in grey and never produce signals.
- **Pagination and rate limits.** The client follows `next_page_token` until the data ends. On HTTP 429 it waits until `X-RateLimit-Reset`. It retries 5xx and network errors with backoff, then reports the failure.
- **Reconnects and late corrections.** The frontend polls every 30 seconds. Each refresh re-fetches the last hour, so a bar Alpaca revises replaces the cached copy. The revision is logged and shown.
- **Missing bars.** When there are no trades on the feed in a bucket, there is no candle. Empty buckets are counted and shown rather than filled in. Exchange holidays and early closes are not modeled; they appear as missing candles.
- **Corporate actions.** Corporate actions come from `/v1/corporate-actions`. Splits, dividends, spin-offs and mergers inside the loaded range are flagged. The chart is marked "CA" at the action date, and any signal whose lookback crosses the date carries a comparability caveat.

## Analysis Rules

All thresholds live in `AnalysisConfig` (`backend/app/config.py`). You can override them with `backend/data/analysis_config.json`. The calculations are deterministic.

### Indicators

| Indicator | Definition |
|---|---|
| EMA 9 and EMA 20 | Seeded with the simple average of the first n closes, then α = 2 / (n + 1) |
| RSI 14 | Wilder smoothing |
| MACD 12/26/9 | EMA(12) − EMA(26); signal line = EMA(9) of MACD; histogram = MACD − signal |
| ATR 14 | Wilder smoothing of true range |
| Session VWAP | Σ(typical price × volume) / Σ volume, reset at each session segment; not used on daily candles |
| Relative volume | Intraday: volume ÷ the average of the same session slot over the previous 10 sessions, with at least 5 required. Daily: volume ÷ the 20-day average |

When there isn't enough history, the value is blank and the app says why.

### Swing Points and Trend

A swing high is a bar whose high is strictly above the 3 bars before it and the 3 bars after it. It exists only after the third bar after it closes; the same applies to swing lows. The trend uses the last two confirmed swing highs and lows:

- **Up:** a higher high and a higher low.
- **Down:** a lower high and a lower low.
- **Range:** a mix of the two.
- **Insufficient:** fewer than two swings of each type.

### Signals

Signals are evaluated on completed candles against the latest swing level confirmed by the previous candle.

| Signal | Rule |
|---|---|
| Early breakout | Close crosses the swing level. No buffer and no volume check, so it is marked as weaker confirmation. |
| Candle-confirmed breakout | Close beyond the level by 0.10 × ATR, with relative volume of at least 1.5. Without the volume, it is recorded as "unconfirmed volume". |
| Retest | Within 10 candles of a confirmed breakout, price returns within 0.25 × ATR of the level and closes on the breakout side. |
| Failed breakout | Within 5 candles of a confirmed breakout, a close goes back through the level by more than the buffer. |

Bearish signals mirror the bullish rules.

### Assessment

The assessment is a fixed rule, not a score:

- **Bullish:** an up trend, a close above EMA 20, and the latest swing low still holding.
- **Bearish:** the mirror image.
- **Neutral:** anything else.

Evidence is listed in separate groups: entry qualification, trend continuation, momentum, momentum warnings, volume, and invalidation. The following are warnings only and never change the label:

- RSI above 70 or below 30.
- A shrinking MACD histogram.

These are never inputs:

- Candle wicks.
- Whether a position is profitable.
- How far a stock has already risen.

### Actionable Confirmation

Actionable confirmation is blocked when any of these apply:

- The fetch failed.
- Data is more than 180 seconds old while a session is open.
- There are fewer than 60 completed candles.
- Indicators lack history.
- More than half of the recent buckets are empty.

The blocking reasons are listed in the app.

## Entry, Exit and Price Projections

- Plan a new trade or manage an existing position. Enter capital, entry price, shares, maximum risk, a stop, and your own targets.
- Position size is the lower of the size your risk budget allows and the size your capital allows.
- P/L includes per-share and per-order fees and slippage.
- The results compare exiting everything at once, selling part and moving the stop to entry, and a trailing stop.
- Levels you enter are drawn as solid lines and labeled "You". Algorithm-derived and calculated levels are dashed and labeled.
- Scenarios (bullish, sideways and bearish) come from observed swing structure. Each lists its trigger, observed targets, invalidation, and the current evidence for and against it. None carries a probability.
- For a saved existing position, the stop can only be tightened. A wider stop is rejected.
- Saved plans record your actual entry and exit, which you enter yourself, for a planned-versus-actual comparison.
- Gaps and slippage can cause losses beyond the planned stop.

## Replay and Backtesting

- Replay recomputes everything from candles up to a chosen time.
- The backtest tests early, candle-confirmed, and retest entries independently, with the same exits, costs, and data checks:
  - Signals fill at the next candle's open, plus slippage.
  - If a candle touches both the stop and the target, the stop is assumed to fill first.
  - If a candle opens beyond the stop, the exit fills at that open.
  - Trailing stops update at each close and only tighten.
  - Re-entry needs a new signal after a cooldown.
- Results are reported separately for in-sample and out-of-sample data, with the out-of-sample portion being the most recent 30% by default. The rules are not tuned on it.
- Premature exits are measured: whether price kept going at least 1R past the exit, and whether price recovered after a stop.
- Signals first seen on a live recent candle are stored with that timestamp. They form a forward paper-test log.
- Do not tune the rules to reproduce one stock's past move. Check any change on other symbols, on the out-of-sample split, and in forward paper testing first.

## Next Trading Day Watchlist

- **Universe.** The scan universe is the official [GoTrade US stock directory](https://www.heygotrade.com/en/us-stock/). Select "Refresh GoTrade directory" to load it. The app stores the page's HTTP `Last-Modified` date and the fetch time. A page that fails or parses to fewer than 100 symbols leaves the previous universe in place.
- **Account availability.** Whether a stock is open to your account is your manual confirmation ("I can trade it in GoTrade" or "Not available to me"). Alpaca tradability is never used as a substitute.
- **Data.** The scanner uses regular-session 30-minute IEX bars. Daily candles are built from them, so the scan, the annotated charts, and both backtests share one dataset. The first full-universe run downloads about 180 days of history and takes several minutes. Later runs only fetch recent bars.
- **Setups.** These rules are evaluated on completed daily candles only. The thresholds live in `ScanConfig` in `backend/app/scanner.py`.

  | Setup | Rule |
  |---|---|
  | Developing breakout | Close within 0.75 ATR below confirmed resistance, above EMA 20, swing low intact, and the 5-day range tightening. Quiet names qualify; no big gain or volume surge is required. |
  | Momentum continuation | Swing trend up, close above EMA 9 above EMA 20, a close in the top quarter of the day's range, and relative volume of at least 1.2. A high daily gain or RSI never rejects a candidate. Extension above EMA 20 is reported and sets the stop distance, and therefore the size. |
  | Uptrend pullback | Swing trend up, swing low intact, at least 1 ATR below the 10-day high, and within 1 ATR of EMA 20. |

- **Candidates.** Each candidate has:
  - An annotated daily chart showing the trigger, invalidation, and targets.
  - The evidence behind the setup.
  - Liquidity: IEX dollar volume and quoted spread, with thresholds.
  - Data-quality status.
  - Catalysts from Alpaca's news API, with source, time, and link. The app checks that the article exists; it does not judge the content.
- **Two separate plans per candidate.**
  - *Overnight to the next opening hour:* entry at today's close, invalidation on the opening print or any 30-minute low before 10:30 ET, and a time exit at 10:30 ET.
  - *Multi-day swing:* entry on a daily close above the trigger, a daily-close invalidation, targets from observed structure, and a 10-day time stop.
- **Count and ranking.**
  - Up to 10 candidates, with at most 4 per setup so quiet setups are not crowded out.
  - Fewer are returned when fewer meet every rule.
  - Rank is an ordering by the number of optional confirmations met. It is not a probability, and no move size is promised.
- **Reassessment.** "Reassess after news and opening gaps" checks the latest IEX trade and any news published after the scan. It marks each plan as one of:
  - plan unchanged
  - invalidated by gap
  - extended: re-plan
  - new news: review
- **Backtests.** Overnight and swing trades are tested separately for each setup, with in-sample and out-of-sample splits. By default they run on a deterministic sample spread across the whole directory rather than hand-picked symbols. Setups are detected using only data available at each day's close.

## Symbols and Positions

No ticker is built into the app. The chart opens on the first symbol in your watchlist, or waits for you to search. No positions are pre-loaded or assumed for any symbol.

## Known Limitations

- Exchange holidays and early closes are not modeled.
- Updates come from REST polling every 30 seconds, not a streaming connection.
- IEX volume is a small share of the market, so relative volume and VWAP are IEX-only measures.
- Symbol search uses Alpaca's asset list. That list is for lookup only; it does not confirm that you can trade a symbol with any broker.

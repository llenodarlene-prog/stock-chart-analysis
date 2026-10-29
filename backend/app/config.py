"""Settings and analysis rules.

Credentials come only from environment variables (or backend/.env, which git
ignores). They are never returned by the API or written to logs.

Every analysis rule is a plain number in AnalysisConfig so results are
deterministic and the active rules can be shown next to every signal.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    feed: str
    data_host: str
    trading_host: str
    database_path: Path

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)


def load_settings() -> Settings:
    db = Path(os.environ.get("DATABASE_PATH", "data/market.sqlite3"))
    if not db.is_absolute():
        db = BACKEND_DIR / db
    return Settings(
        api_key=os.environ.get("ALPACA_API_KEY", "").strip(),
        api_secret=os.environ.get("ALPACA_API_SECRET", "").strip(),
        feed=os.environ.get("ALPACA_FEED", "iex").strip() or "iex",
        data_host=os.environ.get("ALPACA_DATA_HOST", "https://data.alpaca.markets"),
        trading_host=os.environ.get("ALPACA_TRADING_HOST", "https://paper-api.alpaca.markets"),
        database_path=db,
    )


@dataclass(frozen=True)
class AnalysisConfig:
    # Swing points: a swing high is a bar whose high is strictly above the
    # highs of `swing_left` bars before it and `swing_right` bars after it.
    # It becomes available only after the `swing_right` bars have closed.
    swing_left: int = 3
    swing_right: int = 3
    ema_fast: int = 9
    ema_slow: int = 20
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    # Relative volume: same intraday slot over the previous N sessions.
    rvol_sessions: int = 10
    rvol_min_samples: int = 5
    rvol_daily_lookback: int = 20
    # Breakouts: close beyond the latest confirmed swing level by
    # `breakout_buffer_atr` x ATR. Early entries use no buffer and no volume.
    breakout_buffer_atr: float = 0.10
    breakout_min_rvol: float = 1.5
    retest_window: int = 10
    retest_tolerance_atr: float = 0.25
    failed_breakout_window: int = 5
    # Momentum warnings never flip the assessment on their own.
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    # Data quality gates for actionable confirmation.
    min_bars_for_signals: int = 60
    stale_fetch_seconds: int = 180
    max_recent_gap_ratio: float = 0.5

    @classmethod
    def load(cls, path: Path | None = None) -> "AnalysisConfig":
        path = path or BACKEND_DIR / "data" / "analysis_config.json"
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BacktestConfig:
    side: str = "long"  # long or short
    entry_variant: str = "confirmed"  # early, confirmed, retest
    exit_mode: str = "full"  # full, partial, trailing
    stop_mode: str = "swing"  # swing (last confirmed opposite swing) or atr
    stop_atr: float = 1.0
    target_r: float = 2.0
    partial_r: float = 1.0
    partial_fraction: float = 0.5
    move_stop_to_breakeven_after_partial: bool = True
    trail_atr: float = 2.0
    risk_per_trade: float = 100.0
    starting_capital: float = 10000.0
    fee_per_share: float = 0.0
    fee_per_order: float = 0.0
    slippage_bps: float = 5.0
    reentry_cooldown_bars: int = 3
    premature_exit_lookahead: int = 10
    out_of_sample_fraction: float = 0.3
    extra: dict = field(default_factory=dict)

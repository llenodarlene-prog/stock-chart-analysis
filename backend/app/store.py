"""SQLite cache for raw 1-minute bars, coverage, revisions, signals, and plans."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .alpaca import Bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m (
  symbol TEXT NOT NULL, feed TEXT NOT NULL, t INTEGER NOT NULL,
  o REAL, h REAL, l REAL, c REAL, v INTEGER, n INTEGER, vw REAL,
  fetched_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (symbol, feed, t));
CREATE TABLE IF NOT EXISTS coverage (
  symbol TEXT NOT NULL, feed TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
  fetched_at REAL NOT NULL, PRIMARY KEY (symbol, feed));
CREATE TABLE IF NOT EXISTS revisions (
  symbol TEXT, feed TEXT, t INTEGER, old TEXT, new TEXT, seen_at REAL);
CREATE TABLE IF NOT EXISTS corporate_actions (
  symbol TEXT PRIMARY KEY, data TEXT, start TEXT, end TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, timeframe TEXT, sessions TEXT,
  kind TEXT, direction TEXT, variant TEXT, bar_time INTEGER, level REAL,
  first_seen_at REAL, actionable INTEGER, blocked_reasons TEXT, rule TEXT,
  explanation TEXT, evidence TEXT, data_hash TEXT,
  UNIQUE (symbol, timeframe, sessions, kind, variant, bar_time));
CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added_at REAL);
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, updated_at REAL, data TEXT);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # Bars -----------------------------------------------------------------
    def upsert_bars(self, symbol: str, feed: str, bars: list[Bar]) -> int:
        """Inserts bars. A changed bar is a late correction: it replaces the
        cached copy, bumps its revision, and is logged. Returns corrections."""
        now = time.time()
        corrections = 0
        for bar in bars:
            row = self.db.execute("SELECT o,h,l,c,v FROM bars_1m WHERE symbol=? AND feed=? AND t=?", (symbol, feed, bar.t)).fetchone()
            new = (bar.o, bar.h, bar.l, bar.c, bar.v)
            if row is None:
                self.db.execute("INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?,?,?,0)", (symbol, feed, bar.t, *new, bar.n, bar.vw, now))
            elif tuple(row) != new:
                corrections += 1
                self.db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?)", (symbol, feed, bar.t, json.dumps(list(row)), json.dumps(list(new)), now))
                self.db.execute("UPDATE bars_1m SET o=?,h=?,l=?,c=?,v=?,n=?,vw=?,fetched_at=?,revision=revision+1 WHERE symbol=? AND feed=? AND t=?", (*new, bar.n, bar.vw, now, symbol, feed, bar.t))
        self.db.commit()
        return corrections

    def bars(self, symbol: str, feed: str, start: int, end: int) -> list[Bar]:
        rows = self.db.execute("SELECT t,o,h,l,c,v,n,vw FROM bars_1m WHERE symbol=? AND feed=? AND t>=? AND t<=? ORDER BY t", (symbol, feed, start, end)).fetchall()
        return [Bar(*row) for row in rows]

    def coverage(self, symbol: str, feed: str) -> dict | None:
        row = self.db.execute("SELECT start,end,fetched_at FROM coverage WHERE symbol=? AND feed=?", (symbol, feed)).fetchone()
        return dict(row) if row else None

    def set_coverage(self, symbol: str, feed: str, start: int, end: int) -> None:
        self.db.execute("INSERT INTO coverage VALUES (?,?,?,?,?) ON CONFLICT(symbol,feed) DO UPDATE SET start=excluded.start,end=excluded.end,fetched_at=excluded.fetched_at", (symbol, feed, start, end, time.time()))
        self.db.commit()

    def revisions(self, symbol: str, feed: str, since: int) -> list[dict]:
        rows = self.db.execute("SELECT t,old,new,seen_at FROM revisions WHERE symbol=? AND feed=? AND t>=? ORDER BY seen_at DESC LIMIT 50", (symbol, feed, since)).fetchall()
        return [dict(r) for r in rows]

    # Corporate actions ----------------------------------------------------
    def cached_actions(self, symbol: str) -> dict | None:
        row = self.db.execute("SELECT data,start,end,fetched_at FROM corporate_actions WHERE symbol=?", (symbol,)).fetchone()
        return {"actions": json.loads(row["data"]), "start": row["start"], "end": row["end"], "fetched_at": row["fetched_at"]} if row else None

    def save_actions(self, symbol: str, actions: list[dict], start: str, end: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO corporate_actions VALUES (?,?,?,?,?)", (symbol, json.dumps(actions), start, end, time.time()))
        self.db.commit()

    # Signals ----------------------------------------------------------------
    def record_signal(self, symbol: str, timeframe: str, sessions: str, signal: dict, data_hash: str) -> None:
        """First time a confirmed signal is seen is kept as its forward-test
        timestamp; later refreshes never overwrite it."""
        self.db.execute(
            "INSERT OR IGNORE INTO signals (symbol,timeframe,sessions,kind,direction,variant,bar_time,level,first_seen_at,actionable,blocked_reasons,rule,explanation,evidence,data_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, timeframe, sessions, signal["kind"], signal["direction"], signal.get("variant", ""), signal["time"], signal.get("level"), time.time(), int(signal.get("actionable", False)), json.dumps(signal.get("blocked_reasons", [])), signal["rule"], signal["explanation"], json.dumps(signal.get("evidence", {})), data_hash),
        )
        self.db.commit()

    def signal_history(self, symbol: str, timeframe: str, sessions: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM signals WHERE symbol=? AND timeframe=? AND sessions=? ORDER BY bar_time DESC LIMIT 200", (symbol, timeframe, sessions)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["blocked_reasons"] = json.loads(item["blocked_reasons"] or "[]")
            item["evidence"] = json.loads(item["evidence"] or "{}")
            out.append(item)
        return out

    # Watchlist and plans ----------------------------------------------------
    def watchlist(self) -> list[str]:
        return [r["symbol"] for r in self.db.execute("SELECT symbol FROM watchlist ORDER BY added_at")]

    def add_watch(self, symbol: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO watchlist VALUES (?,?)", (symbol, time.time()))
        self.db.commit()

    def remove_watch(self, symbol: str) -> None:
        self.db.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
        self.db.commit()

    def save_plan(self, data: dict, plan_id: int | None = None) -> int:
        now = time.time()
        if plan_id:
            self.db.execute("UPDATE plans SET data=?, updated_at=? WHERE id=?", (json.dumps(data), now, plan_id))
        else:
            plan_id = self.db.execute("INSERT INTO plans (created_at, updated_at, data) VALUES (?,?,?)", (now, now, json.dumps(data))).lastrowid
        self.db.commit()
        return int(plan_id)

    def plans(self) -> list[dict]:
        return [{"id": r["id"], "created_at": r["created_at"], "updated_at": r["updated_at"], **json.loads(r["data"])} for r in self.db.execute("SELECT * FROM plans ORDER BY updated_at DESC")]

    def delete_plan(self, plan_id: int) -> None:
        self.db.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        self.db.commit()

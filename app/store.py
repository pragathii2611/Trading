"""SQLite persistence: trades, equity curve, event log, and key/value state."""
from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    order_id TEXT, time TEXT, symbol TEXT, exchange TEXT, side TEXT, qty INTEGER,
    price REAL, product TEXT, charges REAL, realized REAL, tag TEXT, mode TEXT
);
CREATE TABLE IF NOT EXISTS equity (time TEXT, equity REAL, day_pnl REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS events (time TEXT, level TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def add_trade(self, t: dict, mode: str) -> None:
        self._exec(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["order_id"], t["time"], t["symbol"], t["exchange"], t["side"], t["qty"], t["price"],
             t["product"], t.get("charges", 0), t.get("realized", 0), t.get("tag", ""), mode),
        )

    def trades(self, limit: int = 200) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY time DESC LIMIT ?", (limit,))

    def add_equity(self, time: str, equity: float, day_pnl: float, mode: str) -> None:
        self._exec("INSERT INTO equity VALUES (?,?,?,?)", (time, equity, day_pnl, mode))

    def equity(self, mode: str, limit: int = 2000) -> list[dict]:
        rows = self._rows("SELECT * FROM equity WHERE mode=? ORDER BY time DESC LIMIT ?", (mode, limit))
        return rows[::-1]

    def add_event(self, time: str, level: str, message: str) -> None:
        self._exec("INSERT INTO events VALUES (?,?,?)", (time, level, message))

    def events(self, limit: int = 100) -> list[dict]:
        return self._rows("SELECT * FROM events ORDER BY time DESC LIMIT ?", (limit,))

    def get(self, key: str, default=None):
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def put(self, key: str, value) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, default=str)))

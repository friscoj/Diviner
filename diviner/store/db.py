from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunRecord:
    id: int
    symbol: str
    timeframe: str
    lookback: int
    pred_len: int
    model_id: str
    params: dict[str, Any]
    started_at: str
    finished_at: str | None
    latency_ms: float | None
    status: str
    origin_bar_time: int | None
    error: str | None = None


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    lookback INTEGER NOT NULL,
                    pred_len INTEGER NOT NULL,
                    model_id TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    latency_ms REAL,
                    status TEXT NOT NULL,
                    origin_bar_time INTEGER,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    bar_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    bar_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    amount REAL,
                    close_lo REAL,
                    close_hi REAL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS actuals (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    bar_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe, bar_time)
                );

                CREATE INDEX IF NOT EXISTS idx_forecasts_run ON forecasts(run_id);
                CREATE INDEX IF NOT EXISTS idx_bars_run ON bars(run_id);
                CREATE INDEX IF NOT EXISTS idx_runs_symbol_tf ON runs(symbol, timeframe, id DESC);

                CREATE TABLE IF NOT EXISTS symbol_history (
                    symbol TEXT PRIMARY KEY,
                    last_used_at TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 1
                );
                """
            )

    def list_symbols(self, limit: int = 100) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT symbol FROM symbol_history
                ORDER BY last_used_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(r["symbol"]) for r in rows]

    def remember_symbol(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if not sym:
            return
        now = _utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO symbol_history (symbol, last_used_at, use_count)
                VALUES (?, ?, 1)
                ON CONFLICT(symbol) DO UPDATE SET
                    last_used_at = excluded.last_used_at,
                    use_count = symbol_history.use_count + 1
                """,
                (sym, now),
            )

    def delete_symbol(self, symbol: str) -> bool:
        sym = symbol.strip().upper()
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM symbol_history WHERE symbol = ?", (sym,))
            return cur.rowcount > 0

    def seed_symbols(self, symbols: list[str]) -> None:
        """Insert defaults only when history is empty."""
        if self.list_symbols(limit=1):
            return
        for sym in symbols:
            self.remember_symbol(sym)

    def start_run(
        self,
        *,
        symbol: str,
        timeframe: str,
        lookback: int,
        pred_len: int,
        model_id: str,
        params: dict[str, Any],
        origin_bar_time: int | None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs (
                    symbol, timeframe, lookback, pred_len, model_id, params_json,
                    started_at, status, origin_bar_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    symbol,
                    timeframe,
                    lookback,
                    pred_len,
                    model_id,
                    json.dumps(params),
                    _utc_now_iso(),
                    origin_bar_time,
                ),
            )
            return int(cur.lastrowid)

    def complete_run(
        self,
        run_id: int,
        *,
        latency_ms: float,
        history_bars: list[dict[str, float | int]],
        forecast_rows: list[dict[str, Any]],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, latency_ms = ?, status = 'ok', error = NULL
                WHERE id = ?
                """,
                (_utc_now_iso(), latency_ms, run_id),
            )
            conn.executemany(
                """
                INSERT INTO bars (run_id, bar_time, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        int(b["time"]),
                        float(b["open"]),
                        float(b["high"]),
                        float(b["low"]),
                        float(b["close"]),
                        float(b["volume"]),
                    )
                    for b in history_bars
                ],
            )
            conn.executemany(
                """
                INSERT INTO forecasts (
                    run_id, bar_time, open, high, low, close, volume, amount, close_lo, close_hi
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        int(r["time"]),
                        float(r["open"]),
                        float(r["high"]),
                        float(r["low"]),
                        float(r["close"]),
                        float(r["volume"]),
                        float(r["amount"]) if r.get("amount") is not None else None,
                        float(r["close_lo"]) if r.get("close_lo") is not None else None,
                        float(r["close_hi"]) if r.get("close_hi") is not None else None,
                    )
                    for r in forecast_rows
                ],
            )

    def fail_run(self, run_id: int, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = 'error', error = ?
                WHERE id = ?
                """,
                (_utc_now_iso(), error, run_id),
            )

    def upsert_actuals(
        self,
        *,
        symbol: str,
        timeframe: str,
        bars: list[dict[str, float | int]],
    ) -> None:
        observed = _utc_now_iso()
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO actuals (
                    symbol, timeframe, bar_time, open, high, low, close, volume, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, bar_time) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    observed_at=excluded.observed_at
                """,
                [
                    (
                        symbol,
                        timeframe,
                        int(b["time"]),
                        float(b["open"]),
                        float(b["high"]),
                        float(b["low"]),
                        float(b["close"]),
                        float(b["volume"]),
                        observed,
                    )
                    for b in bars
                ],
            )

    def latest_run(self, symbol: str, timeframe: str) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE symbol = ? AND timeframe = ? AND status = 'ok'
                ORDER BY id DESC LIMIT 1
                """,
                (symbol, timeframe),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def get_run(self, run_id: int) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, symbol: str | None = None, timeframe: str | None = None, limit: int = 50) -> list[RunRecord]:
        clauses = ["1=1"]
        args: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            args.append(symbol)
        if timeframe:
            clauses.append("timeframe = ?")
            args.append(timeframe)
        args.append(limit)
        sql = f"""
            SELECT * FROM runs
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC LIMIT ?
        """
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._row_to_run(r) for r in rows]

    def get_bars(self, run_id: int) -> list[dict[str, float | int]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT bar_time, open, high, low, close, volume FROM bars WHERE run_id = ? ORDER BY bar_time",
                (run_id,),
            ).fetchall()
        return [
            {
                "time": int(r["bar_time"]),
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
            }
            for r in rows
        ]

    def get_forecasts(self, run_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT bar_time, open, high, low, close, volume, amount, close_lo, close_hi
                FROM forecasts WHERE run_id = ? ORDER BY bar_time
                """,
                (run_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "time": int(r["bar_time"]),
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                    "amount": r["amount"],
                    "close_lo": r["close_lo"],
                    "close_hi": r["close_hi"],
                }
            )
        return out

    def accuracy_for_run(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            return {"success": False, "error": "run not found"}
        forecasts = self.get_forecasts(run_id)
        if not forecasts:
            return {"success": False, "error": "no forecasts"}
        times = [f["time"] for f in forecasts]
        placeholders = ",".join("?" * len(times))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT bar_time, close FROM actuals
                WHERE symbol = ? AND timeframe = ? AND bar_time IN ({placeholders})
                """,
                [run.symbol, run.timeframe, *times],
            ).fetchall()
        actual_map = {int(r["bar_time"]): float(r["close"]) for r in rows}
        pairs = []
        for f in forecasts:
            a = actual_map.get(int(f["time"]))
            if a is None:
                continue
            pairs.append((float(f["close"]), a))
        if not pairs:
            return {
                "success": True,
                "run_id": run_id,
                "matched": 0,
                "mae_close": None,
                "direction_hit_rate": None,
            }
        mae = sum(abs(p - a) for p, a in pairs) / len(pairs)
        # Direction vs previous forecast step (or skip first)
        hits = 0
        dir_n = 0
        for i in range(1, len(pairs)):
            pred_dir = pairs[i][0] - pairs[i - 1][0]
            act_dir = pairs[i][1] - pairs[i - 1][1]
            if pred_dir == 0 or act_dir == 0:
                continue
            dir_n += 1
            if (pred_dir > 0) == (act_dir > 0):
                hits += 1
        return {
            "success": True,
            "run_id": run_id,
            "matched": len(pairs),
            "mae_close": mae,
            "direction_hit_rate": (hits / dir_n) if dir_n else None,
        }

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=int(row["id"]),
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            lookback=int(row["lookback"]),
            pred_len=int(row["pred_len"]),
            model_id=row["model_id"],
            params=json.loads(row["params_json"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            latency_ms=row["latency_ms"],
            status=row["status"],
            origin_bar_time=row["origin_bar_time"],
            error=row["error"],
        )

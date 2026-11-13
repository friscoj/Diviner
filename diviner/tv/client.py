from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diviner.calendar import resolution_seconds
from diviner.config import Settings


class TvError(RuntimeError):
    """TradingView CLI / CDP failure."""

    def __init__(self, message: str, *, exit_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.payload = payload


@dataclass(frozen=True)
class Bar:
    time: int  # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class TvClient:
    """Thin wrapper around tradingview-mcp `tv` CLI (JSON stdout)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._history_ready_for: tuple[str, str, int] | None = None

    def _run(self, *args: str, timeout: float = 60.0) -> dict[str, Any]:
        cmd = [self.settings.tv_node, self.settings.tv_cli, *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TvError(f"Failed to launch TV CLI: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TvError(f"TV CLI timed out after {timeout}s: {' '.join(args)}") from exc

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        payload: Any = None
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = {"raw": stdout}

        if proc.returncode != 0:
            err = None
            if isinstance(payload, dict):
                err = payload.get("error")
            if not err and stderr:
                try:
                    err_payload = json.loads(stderr)
                    if isinstance(err_payload, dict):
                        err = err_payload.get("error")
                        payload = err_payload
                except json.JSONDecodeError:
                    err = stderr
            raise TvError(
                err or f"TV CLI failed ({proc.returncode}): {' '.join(args)}",
                exit_code=proc.returncode,
                payload=payload,
            )

        if not isinstance(payload, dict):
            raise TvError(f"Unexpected TV CLI output for {' '.join(args)}", payload=payload)
        return payload

    def status(self) -> dict[str, Any]:
        return self._run("status", timeout=20.0)

    def connected(self) -> bool:
        try:
            result = self.status()
            return bool(result.get("success") and result.get("cdp_connected"))
        except TvError:
            return False

    def state(self) -> dict[str, Any]:
        return self._run("state", timeout=20.0)

    def set_symbol(self, symbol: str) -> dict[str, Any]:
        return self._run("symbol", symbol, timeout=45.0)

    def set_timeframe(self, timeframe: str) -> dict[str, Any]:
        return self._run("timeframe", timeframe, timeout=30.0)

    def ensure_chart(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """Switch active chart to symbol/TF if needed, then return state."""
        state = self.state()
        current_symbol = state.get("symbol") or ""
        current_tf = str(state.get("resolution") or "")
        if current_symbol != symbol:
            self.set_symbol(symbol)
        if current_tf != str(timeframe):
            self.set_timeframe(timeframe)
        return self.state()

    def ohlcv(self, count: int = 400) -> list[Bar]:
        if count < 1 or count > 500:
            raise ValueError("count must be between 1 and 500 (TV limit)")
        result = self._run("ohlcv", "-n", str(count), timeout=45.0)
        bars_raw = result.get("bars")
        if not isinstance(bars_raw, list):
            raise TvError("ohlcv response missing bars[]", payload=result)
        bars: list[Bar] = []
        for row in bars_raw:
            bars.append(
                Bar(
                    time=int(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                )
            )
        bars.sort(key=lambda b: b.time)
        return bars

    def set_visible_range(self, from_ts: int, to_ts: int) -> dict[str, Any]:
        """Set chart visible range; TV pages in older bars via requestMoreData."""
        return self._run(
            "range",
            "--from",
            str(int(from_ts)),
            "--to",
            str(int(to_ts)),
            timeout=120.0,
        )

    def ensure_history(self, lookback: int, timeframe: str) -> None:
        """
        TradingView charts lazy-load ~300 bars. Page backward until enough
        history exists for `lookback` (+ forming-bar slack).
        """
        res_sec = resolution_seconds(timeframe)
        # Peek at whatever is already loaded.
        peek = self.ohlcv(count=min(50, max(lookback, 10)))
        if not peek:
            raise TvError("No OHLCV bars available on the active chart")

        need = lookback + 5  # slack for forming-bar drop
        to_ts = peek[-1].time + res_sec
        # Session gaps / weekends: request several× wall-clock span.
        span = need * res_sec * 4
        from_ts = to_ts - span

        self.set_visible_range(from_ts, to_ts)

        # If still short, widen once more (Globex gaps can be large).
        bars = self.ohlcv(count=min(500, need))
        if len(bars) < need:
            from_ts = to_ts - span * 2
            self.set_visible_range(from_ts, to_ts)

    def fetch_lookback(self, symbol: str, timeframe: str, lookback: int) -> list[Bar]:
        """Set chart, page in history if needed, return bars for Kronos lookback."""
        self.ensure_chart(symbol, timeframe)
        request = min(500, lookback + 5)
        key = (symbol, str(timeframe), lookback)
        bars = self.ohlcv(count=request)

        # Charts start with ~300 bars; page older data once per symbol/TF.
        if len(bars) < request and self._history_ready_for != key:
            self.ensure_history(lookback, timeframe)
            self._history_ready_for = key
            bars = self.ohlcv(count=request)

        if len(bars) < lookback:
            # Allow one more forced page-in on failure.
            self.ensure_history(lookback, timeframe)
            self._history_ready_for = key
            bars = self.ohlcv(count=request)

        if len(bars) < lookback:
            raise TvError(
                f"Only {len(bars)} bars available after history load "
                f"(need {lookback}). Scroll the TV chart further left or "
                f"wait for more data to load, then retry."
            )
        return bars

    def _load_draw_ids(self) -> list[str]:
        path = Path(self.settings.tv_draw_ids_path)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text())
            ids = data.get("entity_ids") if isinstance(data, dict) else data
            return [str(x) for x in (ids or [])]
        except (OSError, json.JSONDecodeError):
            return []

    def _save_draw_ids(self, entity_ids: list[str]) -> None:
        path = Path(self.settings.tv_draw_ids_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entity_ids": entity_ids}, indent=2))

    def draw_prediction_polyline(
        self,
        points: list[dict[str, float | int]],
        *,
        color: str | None = None,
        width: int | None = None,
    ) -> dict[str, Any]:
        """
        Draw forecast path as consecutive trend_line segments on the active chart.
        Removes previously drawn Diviner segments first.
        """
        if len(points) < 2:
            raise TvError("Need at least 2 forecast points to draw a polyline")

        script = Path(self.settings.polyline_script)
        if not script.is_file():
            raise TvError(f"Polyline script missing: {script}")

        payload = {
            "points": [
                {"time": int(p["time"]), "price": float(p["price"])} for p in points
            ],
            "remove_ids": self._load_draw_ids(),
            "color": color or self.settings.prediction_line_color,
            "width": int(width or self.settings.prediction_line_width),
        }

        # ~0.05s per segment + CDP overhead; 120 pts ≈ generous buffer
        timeout = max(120.0, 2.0 * len(points))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name

        cmd = [self.settings.tv_node, str(script), tmp_path]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Script may have finished drawing + printed JSON but hung on CDP teardown.
            stdout_partial = exc.stdout or ""
            if isinstance(stdout_partial, bytes):
                stdout_partial = stdout_partial.decode("utf-8", errors="replace")
            result = self._parse_polyline_stdout(stdout_partial)
            if result.get("success"):
                entity_ids = [str(x) for x in (result.get("entity_ids") or [])]
                self._save_draw_ids(entity_ids)
                return result
            raise TvError(f"Drawing polyline timed out after {timeout}s") from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        result = self._parse_polyline_stdout(proc.stdout or "")
        if proc.returncode != 0 or not result.get("success"):
            raise TvError(
                result.get("error") or "Failed to draw prediction on TradingView",
                exit_code=proc.returncode,
                payload=result,
            )

        entity_ids = [str(x) for x in (result.get("entity_ids") or [])]
        self._save_draw_ids(entity_ids)
        return result

    @staticmethod
    def _parse_polyline_stdout(stdout: str) -> dict[str, Any]:
        text = (stdout or "").strip()
        if not text:
            return {}
        # Prefer the last JSON object line (ignore any incidental logs).
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TvError(f"Bad polyline script output: {text[:500]}") from exc
        return parsed if isinstance(parsed, dict) else {}


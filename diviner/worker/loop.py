from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from diviner.config import Settings
from diviner.kronos.engine import KronosEngine
from diviner.store.db import Store
from diviner.tv.client import TvClient, TvError

log = logging.getLogger("diviner.worker")

BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class SessionState:
    symbol: str = "ES1!"
    timeframe: str = "5"
    update_seconds: float = 10.0
    temperature: float = 1.0
    top_p: float = 0.9
    sample_count: int = 1
    band_samples: int = 0
    last_origin_bar_time: int | None = None
    last_run_id: int | None = None
    last_error: str | None = None
    predicting: bool = False
    tv_connected: bool = False
    model_ready: bool = False


@dataclass
class ForecastWorker:
    settings: Settings
    tv: TvClient
    engine: KronosEngine
    store: Store
    broadcast: BroadcastFn
    state: SessionState = field(default_factory=SessionState)
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _kick: asyncio.Event = field(default_factory=asyncio.Event)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="diviner-worker")

    async def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        if self._task:
            await self._task

    def configure(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        update_seconds: float | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        sample_count: int | None = None,
        band_samples: int | None = None,
        force: bool = False,
    ) -> SessionState:
        if symbol is not None:
            symbol = symbol.strip().upper()
            if not symbol:
                raise ValueError("Symbol must be a non-empty TradingView symbol")
            if symbol != self.state.symbol:
                self.state.symbol = symbol
                self.state.last_origin_bar_time = None
                force = True
            self.store.remember_symbol(symbol)
        if timeframe is not None:
            timeframe = str(timeframe).strip()
            if timeframe not in self.settings.timeframes:
                raise ValueError(
                    f"Unsupported timeframe: {timeframe}. "
                    f"Choose one of: {', '.join(self.settings.timeframes)}"
                )
            if timeframe != self.state.timeframe:
                self.state.timeframe = timeframe
                self.state.last_origin_bar_time = None
                force = True
        if update_seconds is not None:
            allowed = {float(x) for x in self.settings.update_intervals}
            if float(update_seconds) not in allowed:
                raise ValueError(
                    f"Unsupported update interval: {update_seconds}. "
                    f"Choose one of: {', '.join(str(x) for x in self.settings.update_intervals)}"
                )
            if float(update_seconds) != self.state.update_seconds:
                self.state.update_seconds = float(update_seconds)
                # Wake the loop so the new cadence applies without forcing a repredict.
                self._kick.set()
        if temperature is not None:
            self.state.temperature = temperature
        if top_p is not None:
            self.state.top_p = top_p
        if sample_count is not None:
            self.state.sample_count = sample_count
        if band_samples is not None:
            self.state.band_samples = band_samples
        if force:
            self._kick.set()
        return self.state

    def request_refresh(self) -> None:
        self.state.last_origin_bar_time = None
        self._kick.set()

    async def _run(self) -> None:
        log.info("Worker started")
        # Load model in a thread so startup doesn't block the event loop forever
        try:
            await asyncio.to_thread(self.engine.load)
            self.state.model_ready = True
            await self.broadcast({"type": "status", "payload": self.status_payload()})
        except Exception as exc:  # noqa: BLE001
            self.state.model_ready = False
            self.state.last_error = f"Model load failed: {exc}"
            log.exception("Model load failed")
            await self.broadcast({"type": "error", "payload": {"error": self.state.last_error}})

        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = str(exc)
                log.exception("Worker tick failed")
                await self.broadcast({"type": "error", "payload": {"error": str(exc)}})

            try:
                await asyncio.wait_for(self._kick.wait(), timeout=self.state.update_seconds)
                self._kick.clear()
            except asyncio.TimeoutError:
                pass

        log.info("Worker stopped")

    def status_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.state.symbol,
            "timeframe": self.state.timeframe,
            "update_seconds": self.state.update_seconds,
            "temperature": self.state.temperature,
            "top_p": self.state.top_p,
            "sample_count": self.state.sample_count,
            "band_samples": self.state.band_samples,
            "last_origin_bar_time": self.state.last_origin_bar_time,
            "last_run_id": self.state.last_run_id,
            "last_error": self.state.last_error,
            "predicting": self.state.predicting,
            "tv_connected": self.state.tv_connected,
            "model_ready": self.state.model_ready,
            "model_id": self.settings.model_id,
            "model_source": getattr(self.engine, "model_source", self.settings.model_id),
            "device": getattr(self.engine, "device", self.settings.device),
            "lookback": self.settings.lookback,
            "pred_len": self.settings.pred_len,
        }

    async def _tick(self) -> None:
        self.state.tv_connected = await asyncio.to_thread(self.tv.connected)
        await self.broadcast({"type": "status", "payload": self.status_payload()})
        if not self.state.tv_connected:
            self.state.last_error = "TradingView CDP not connected"
            return
        if not self.state.model_ready:
            return

        symbol = self.state.symbol
        timeframe = self.state.timeframe

        try:
            bars = await asyncio.to_thread(
                self.tv.fetch_lookback,
                symbol,
                timeframe,
                self.settings.lookback,
            )
        except TvError as exc:
            self.state.last_error = str(exc)
            await self.broadcast({"type": "error", "payload": {"error": str(exc)}})
            return

        if not bars:
            self.state.last_error = "No bars returned from TradingView"
            return

        # Persist actuals for accuracy (all fetched bars)
        await asyncio.to_thread(
            self.store.upsert_actuals,
            symbol=symbol,
            timeframe=timeframe,
            bars=[b.to_dict() for b in bars],
        )

        # Origin = last closed bar if drop_forming else last bar
        origin_bar = bars[-2] if self.settings.drop_forming_bar and len(bars) >= 2 else bars[-1]
        origin_time = origin_bar.time

        if (
            self.state.last_origin_bar_time is not None
            and origin_time == self.state.last_origin_bar_time
        ):
            # Still broadcast live candles so chart stays fresh
            await self.broadcast(
                {
                    "type": "bars",
                    "payload": {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "bars": [b.to_dict() for b in bars],
                    },
                }
            )
            return

        await self._predict_and_publish(bars, origin_time)

    async def _predict_and_publish(self, bars, origin_time: int) -> None:
        symbol = self.state.symbol
        timeframe = self.state.timeframe
        self.state.predicting = True
        await self.broadcast({"type": "status", "payload": self.status_payload()})

        run_id = self.store.start_run(
            symbol=symbol,
            timeframe=timeframe,
            lookback=self.settings.lookback,
            pred_len=self.settings.pred_len,
            model_id=self.settings.model_id,
            params={
                "temperature": self.state.temperature,
                "top_p": self.state.top_p,
                "sample_count": self.state.sample_count,
                "band_samples": self.state.band_samples,
            },
            origin_bar_time=origin_time,
        )

        try:
            result = await asyncio.to_thread(
                self.engine.forecast,
                bars,
                timeframe=timeframe,
                temperature=self.state.temperature,
                top_p=self.state.top_p,
                sample_count=self.state.sample_count,
                band_samples=self.state.band_samples,
            )
            self.store.complete_run(
                run_id,
                latency_ms=result.latency_ms,
                history_bars=result.history,
                forecast_rows=result.forecast,
            )
            self.state.last_origin_bar_time = result.origin_bar_time
            self.state.last_run_id = run_id
            self.state.last_error = None
            self.state.predicting = False
            # Notify clients with a small message; they fetch /api/latest over HTTP
            # (FastAPI JSON encoder). Huge WS payloads with NaN were silently dropped.
            await self.broadcast(
                {
                    "type": "forecast_ready",
                    "payload": {
                        "run_id": run_id,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "origin_bar_time": result.origin_bar_time,
                        "latency_ms": result.latency_ms,
                    },
                }
            )
            await self.broadcast({"type": "status", "payload": self.status_payload()})
            log.info(
                "Forecast run=%s %s/%s origin=%s latency=%.0fms",
                run_id,
                symbol,
                timeframe,
                result.origin_bar_time,
                result.latency_ms,
            )
        except Exception as exc:  # noqa: BLE001
            self.store.fail_run(run_id, str(exc))
            self.state.predicting = False
            self.state.last_error = str(exc)
            log.exception("Forecast failed")
            await self.broadcast({"type": "error", "payload": {"error": str(exc), "run_id": run_id}})
            await self.broadcast({"type": "status", "payload": self.status_payload()})

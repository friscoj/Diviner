from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import json

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from diviner.config import load_settings
from diviner.kronos.engine import KronosEngine
from diviner.store.db import Store
from diviner.tv.client import TvClient, TvError
from diviner.tv.overlay import draw_run_prediction
from diviner.worker.loop import ForecastWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("diviner.api")


class ConfigureBody(BaseModel):
    symbol: str | None = None
    timeframe: str | None = None
    update_seconds: float | None = Field(default=None, ge=1.0, le=3600.0)
    temperature: float | None = Field(default=None, ge=0.0, le=5.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    sample_count: int | None = Field(default=None, ge=1, le=16)
    band_samples: int | None = Field(default=None, ge=0, le=16)
    force: bool = False


class DrawPredictionBody(BaseModel):
    run_id: int | None = None
    symbol: str | None = None
    timeframe: str | None = None


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        # jsonable_encoder turns NaN/Inf into null — raw json.dumps does not,
        # and browsers then fail JSON.parse and drop the update.
        text = json.dumps(jsonable_encoder(message), allow_nan=False)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


def create_app() -> FastAPI:
    settings = load_settings()
    store = Store(settings.db_path)
    store.seed_symbols(settings.symbol_suggestions)
    tv = TvClient(settings)
    engine = KronosEngine(settings)
    hub = Hub()
    worker = ForecastWorker(
        settings=settings,
        tv=tv,
        engine=engine,
        store=store,
        broadcast=hub.broadcast,
    )
    worker.state.update_seconds = float(settings.poll_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        yield
        await worker.stop()

    app = FastAPI(title="Diviner", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.tv = tv
    app.state.engine = engine
    app.state.worker = worker
    app.state.hub = hub

    web_dir: Path = settings.web_dir

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        tv_ok = await asyncio.to_thread(tv.connected)
        return {
            "ok": True,
            "tv_connected": tv_ok,
            "model_ready": engine.ready,
            "model_error": engine.load_error,
            "worker": worker.status_payload(),
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "symbols": store.list_symbols(),
            "timeframes": settings.timeframes,
            "update_intervals": settings.update_intervals,
            "lookback": settings.lookback,
            "pred_len": settings.pred_len,
            "model_id": settings.model_id,
            "defaults": {
                "symbol": worker.state.symbol or settings.default_symbol,
                "timeframe": worker.state.timeframe,
                "update_seconds": worker.state.update_seconds,
                "temperature": worker.state.temperature,
                "top_p": worker.state.top_p,
                "sample_count": worker.state.sample_count,
                "band_samples": worker.state.band_samples,
            },
        }

    @app.get("/api/symbols")
    async def list_symbols() -> dict[str, Any]:
        return {"symbols": store.list_symbols()}

    @app.delete("/api/symbols/{symbol}")
    async def delete_symbol(symbol: str) -> dict[str, Any]:
        deleted = store.delete_symbol(symbol)
        if not deleted:
            raise HTTPException(status_code=404, detail="symbol not in history")
        return {"ok": True, "symbols": store.list_symbols()}

    @app.post("/api/configure")
    async def configure(body: ConfigureBody) -> dict[str, Any]:
        try:
            state = worker.configure(
                symbol=body.symbol,
                timeframe=body.timeframe,
                update_seconds=body.update_seconds,
                temperature=body.temperature,
                top_p=body.top_p,
                sample_count=body.sample_count,
                band_samples=body.band_samples,
                force=body.force,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await hub.broadcast({"type": "status", "payload": worker.status_payload()})
        return {
            "ok": True,
            "state": worker.status_payload(),
            "session": state.__dict__,
            "symbols": store.list_symbols(),
        }

    @app.post("/api/refresh")
    async def refresh() -> dict[str, Any]:
        worker.request_refresh()
        return {"ok": True}

    @app.post("/api/draw-prediction")
    async def draw_prediction(body: DrawPredictionBody) -> dict[str, Any]:
        """Draw the current (or specified) forecast path onto TradingView Desktop."""
        run_id = body.run_id
        symbol = (body.symbol or worker.state.symbol or "").strip().upper() or None
        timeframe = (body.timeframe or worker.state.timeframe or "").strip() or None
        try:
            result = await asyncio.to_thread(
                draw_run_prediction,
                store=store,
                tv=tv,
                run_id=run_id,
                symbol=symbol,
                timeframe=timeframe,
            )
        except TvError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("draw-prediction failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return result

    @app.get("/api/runs")
    async def list_runs(symbol: str | None = None, timeframe: str | None = None, limit: int = 50):
        runs = store.list_runs(symbol=symbol, timeframe=timeframe, limit=limit)
        return {
            "runs": [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "timeframe": r.timeframe,
                    "status": r.status,
                    "latency_ms": r.latency_ms,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "origin_bar_time": r.origin_bar_time,
                    "params": r.params,
                    "error": r.error,
                }
                for r in runs
            ]
        }

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: int):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run": {
                "id": run.id,
                "symbol": run.symbol,
                "timeframe": run.timeframe,
                "status": run.status,
                "latency_ms": run.latency_ms,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "origin_bar_time": run.origin_bar_time,
                "params": run.params,
                "error": run.error,
                "model_id": run.model_id,
                "lookback": run.lookback,
                "pred_len": run.pred_len,
            },
            "history": store.get_bars(run_id),
            "forecast": store.get_forecasts(run_id),
        }

    @app.get("/api/runs/{run_id}/accuracy")
    async def run_accuracy(run_id: int):
        return store.accuracy_for_run(run_id)

    @app.get("/api/latest")
    async def latest(symbol: str | None = None, timeframe: str | None = None):
        sym = symbol or worker.state.symbol
        tf = timeframe or worker.state.timeframe
        run = store.latest_run(sym, tf)
        if not run:
            return {"run": None, "history": [], "forecast": []}
        return {
            "run": {
                "id": run.id,
                "symbol": run.symbol,
                "timeframe": run.timeframe,
                "latency_ms": run.latency_ms,
                "origin_bar_time": run.origin_bar_time,
                "params": run.params,
                "finished_at": run.finished_at,
            },
            "history": store.get_bars(run.id),
            "forecast": store.get_forecasts(run.id),
        }

    async def _ws_send(ws: WebSocket, message: dict[str, Any]) -> None:
        await ws.send_text(json.dumps(jsonable_encoder(message), allow_nan=False))

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await hub.connect(ws)
        try:
            await _ws_send(ws, {"type": "status", "payload": worker.status_payload()})
            # Client loads chart data via HTTP /api/latest (reliable JSON).
            latest = store.latest_run(worker.state.symbol, worker.state.timeframe)
            if latest:
                await _ws_send(
                    ws,
                    {
                        "type": "forecast_ready",
                        "payload": {
                            "run_id": latest.id,
                            "symbol": latest.symbol,
                            "timeframe": latest.timeframe,
                            "origin_bar_time": latest.origin_bar_time,
                            "latency_ms": latest.latency_ms,
                        },
                    },
                )
            while True:
                # Keep alive; ignore client messages for now (config via REST)
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await hub.disconnect(ws)

    @app.get("/")
    async def index():
        index_path = web_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="web UI missing")
        return FileResponse(index_path)

    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

    return app


app = create_app()

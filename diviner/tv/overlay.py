"""Push Kronos forecasts onto the TradingView chart as drawings."""

from __future__ import annotations

from typing import Any

from diviner.store.db import Store
from diviner.tv.client import TvClient, TvError


def forecast_to_close_points(forecast_rows: list[dict[str, Any]]) -> list[dict[str, float | int]]:
    points: list[dict[str, float | int]] = []
    for row in forecast_rows:
        points.append({"time": int(row["time"]), "price": float(row["close"])})
    return points


def draw_run_prediction(
    *,
    store: Store,
    tv: TvClient,
    run_id: int | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """
    Draw predicted close path for a run onto the active TradingView chart.
    Uses latest successful run for symbol/TF when run_id is omitted.
    """
    if run_id is not None:
        run = store.get_run(run_id)
        if not run or run.status != "ok":
            raise TvError(f"Run {run_id} not found or not successful")
    else:
        if not symbol or not timeframe:
            raise TvError("symbol and timeframe required when run_id is omitted")
        run = store.latest_run(symbol, timeframe)
        if not run:
            raise TvError(f"No successful forecast for {symbol} @ {timeframe}")

    forecast = store.get_forecasts(run.id)
    points = forecast_to_close_points(forecast)
    if len(points) < 2:
        raise TvError("Forecast has fewer than 2 points")

    # Align chart to the run's instrument before drawing.
    tv.ensure_chart(run.symbol, run.timeframe)
    result = tv.draw_prediction_polyline(points)
    return {
        "ok": True,
        "run_id": run.id,
        "symbol": run.symbol,
        "timeframe": run.timeframe,
        "points": len(points),
        "segments": result.get("segments"),
        "entity_ids": result.get("entity_ids") or [],
        "color": result.get("color"),
    }

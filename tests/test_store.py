from pathlib import Path

from diviner.store.db import Store


def test_store_roundtrip(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    run_id = store.start_run(
        symbol="ES1!",
        timeframe="5",
        lookback=400,
        pred_len=120,
        model_id="NeoQuasar/Kronos-base",
        params={"temperature": 1.0},
        origin_bar_time=1_700_000_000,
    )
    history = [
        {"time": 1_700_000_000 - 300, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
        {"time": 1_700_000_000, "open": 1.5, "high": 2.2, "low": 1.4, "close": 2.0, "volume": 12},
    ]
    forecast = [
        {
            "time": 1_700_000_300,
            "open": 2.0,
            "high": 2.3,
            "low": 1.9,
            "close": 2.1,
            "volume": 11,
            "amount": 22.0,
            "close_lo": 2.05,
            "close_hi": 2.15,
        }
    ]
    store.complete_run(run_id, latency_ms=123.4, history_bars=history, forecast_rows=forecast)
    latest = store.latest_run("ES1!", "5")
    assert latest is not None
    assert latest.id == run_id
    assert latest.latency_ms == 123.4
    assert len(store.get_bars(run_id)) == 2
    fc = store.get_forecasts(run_id)
    assert len(fc) == 1
    assert fc[0]["close_hi"] == 2.15

    store.upsert_actuals(
        symbol="ES1!",
        timeframe="5",
        bars=[{"time": 1_700_000_300, "open": 2, "high": 2.4, "low": 1.8, "close": 2.2, "volume": 9}],
    )
    acc = store.accuracy_for_run(run_id)
    assert acc["matched"] == 1
    assert abs(acc["mae_close"] - 0.1) < 1e-9

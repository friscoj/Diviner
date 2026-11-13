from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from diviner.calendar import future_bar_timestamps
from diviner.config import Settings
from diviner.tv.client import Bar

log = logging.getLogger("diviner.kronos")


def _finite(value: Any, default: float = 0.0) -> float:
    """JSON-safe float (NaN/Inf break WebSocket json.dumps → client drops updates)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(out):
        return default
    return out


@dataclass
class ForecastResult:
    history: list[dict[str, float | int]]
    forecast: list[dict[str, Any]]
    origin_bar_time: int
    latency_ms: float
    params: dict[str, Any]


class KronosEngine:
    """Lazy-loads Kronos-base once and runs forecasts on GPU."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._predictor = None
        self._load_error: str | None = None
        self.device = settings.device
        self.model_source = str(settings.model_id)
        self.tokenizer_source = str(settings.tokenizer_id)

    @property
    def ready(self) -> bool:
        return self._predictor is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def load(self) -> None:
        if self._predictor is not None:
            return
        try:
            kronos_root = str(self.settings.kronos_root)
            if kronos_root not in sys.path:
                sys.path.insert(0, kronos_root)
            from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore

            import torch

            device = self.settings.device
            if device == "auto":
                if torch.cuda.is_available():
                    device = "cuda:0"
                else:
                    device = "cpu"
                    log.warning(
                        "CUDA not available (torch.cuda.is_available()=False); "
                        "loading Kronos on CPU. If you expected a GPU, try "
                        "`nvidia-smi`, reset the GPU, or reboot, then restart Diviner."
                    )

            tok_src = self.settings.resolved_tokenizer_path()
            model_src = self.settings.resolved_model_path()
            local_only = isinstance(tok_src, Path) and isinstance(model_src, Path)
            load_kw = {"local_files_only": True} if local_only else {}
            log.info(
                "Loading Kronos from %s (local_files_only=%s) on %s",
                model_src,
                local_only,
                device,
            )

            tokenizer = KronosTokenizer.from_pretrained(str(tok_src), **load_kw)
            model = Kronos.from_pretrained(str(model_src), **load_kw)
            self._predictor = KronosPredictor(
                model,
                tokenizer,
                device=device,
                max_context=self.settings.max_context,
            )
            self.device = device
            self.model_source = str(model_src)
            self.tokenizer_source = str(tok_src)
            self._load_error = None
        except Exception as exc:  # noqa: BLE001 — surface to UI
            self._load_error = str(exc)
            raise

    def _bars_to_frame(
        self, bars: list[Bar], *, drop_forming: bool
    ) -> tuple[pd.DataFrame, pd.Series, list[Bar]]:
        use = list(bars)
        if drop_forming and len(use) >= 2:
            # Drop potentially incomplete last bar
            use = use[:-1]
        if len(use) < self.settings.lookback:
            raise ValueError(
                f"Need {self.settings.lookback} closed bars, got {len(use)} "
                f"(fetched {len(bars)}; forming drop={drop_forming})"
            )
        use = use[-self.settings.lookback :]
        tz = self.settings.timezone
        times = pd.to_datetime([b.time for b in use], unit="s", utc=True).tz_convert(tz)
        df = pd.DataFrame(
            {
                "open": [b.open for b in use],
                "high": [b.high for b in use],
                "low": [b.low for b in use],
                "close": [b.close for b in use],
                "volume": [b.volume for b in use],
            }
        )
        return df, pd.Series(times), use

    def _predict_once(
        self,
        df: pd.DataFrame,
        x_timestamp: pd.Series,
        y_timestamp: pd.Series,
        *,
        temperature: float,
        top_p: float,
        sample_count: int,
    ) -> pd.DataFrame:
        assert self._predictor is not None
        return self._predictor.predict(
            df=df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=self.settings.pred_len,
            T=temperature,
            top_p=top_p,
            sample_count=sample_count,
            verbose=False,
        )

    def forecast(
        self,
        bars: list[Bar],
        *,
        timeframe: str,
        temperature: float | None = None,
        top_p: float | None = None,
        sample_count: int | None = None,
        band_samples: int = 0,
    ) -> ForecastResult:
        if self._predictor is None:
            self.load()

        temperature = self.settings.temperature if temperature is None else temperature
        top_p = self.settings.top_p if top_p is None else top_p
        sample_count = self.settings.sample_count if sample_count is None else sample_count

        df, x_timestamp, used = self._bars_to_frame(
            bars, drop_forming=self.settings.drop_forming_bar
        )
        origin = used[-1]
        origin_dt = datetime.fromtimestamp(origin.time, tz=x_timestamp.dt.tz)
        y_index = future_bar_timestamps(
            origin_dt,
            resolution=timeframe,
            count=self.settings.pred_len,
            tz=self.settings.timezone,
        )
        # Kronos calc_time_stamps uses `.dt` — requires Series, not DatetimeIndex.
        y_timestamp = pd.Series(y_index)

        t0 = time.perf_counter()
        pred = self._predict_once(
            df,
            x_timestamp,
            y_timestamp,
            temperature=temperature,
            top_p=top_p,
            sample_count=max(1, sample_count),
        )

        close_lo = None
        close_hi = None
        if band_samples and band_samples > 1:
            closes = [pred["close"].to_numpy(dtype=float)]
            for _ in range(band_samples - 1):
                extra = self._predict_once(
                    df,
                    x_timestamp,
                    y_timestamp,
                    temperature=temperature,
                    top_p=top_p,
                    sample_count=1,
                )
                closes.append(extra["close"].to_numpy(dtype=float))
            stack = np.vstack(closes)
            close_lo = stack.min(axis=0)
            close_hi = stack.max(axis=0)
            # Replace primary close with mean of band samples for consistency
            pred = pred.copy()
            pred["close"] = stack.mean(axis=0)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        forecast_rows: list[dict[str, Any]] = []
        for i, (_, row) in enumerate(pred.iterrows()):
            ts = y_index[i]
            t_unix = int(pd.Timestamp(ts).timestamp())
            amount = None
            if "amount" in row.index:
                amount = _finite(row["amount"])
            item: dict[str, Any] = {
                "time": t_unix,
                "open": _finite(row["open"]),
                "high": _finite(row["high"]),
                "low": _finite(row["low"]),
                "close": _finite(row["close"]),
                "volume": _finite(row["volume"]),
                "amount": amount,
            }
            if close_lo is not None and close_hi is not None:
                item["close_lo"] = _finite(close_lo[i])
                item["close_hi"] = _finite(close_hi[i])
            forecast_rows.append(item)

        history = [b.to_dict() for b in used]
        params = {
            "temperature": temperature,
            "top_p": top_p,
            "sample_count": sample_count,
            "band_samples": band_samples,
        }
        return ForecastResult(
            history=history,
            forecast=forecast_rows,
            origin_bar_time=origin.time,
            latency_ms=latency_ms,
            params=params,
        )

"""CME Globex session helpers for equity-index futures (ES / NQ).

Schedule (America/Chicago):
  - Open: Sunday 17:00 CT through Friday 16:00 CT
  - Daily break: 16:00–17:00 CT (Mon–Thu)
  - Closed: Friday 16:00 CT → Sunday 17:00 CT

v1 holiday handling is intentionally light (full weekend/daily break only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

CT = ZoneInfo("America/Chicago")

_SESSION_OPEN_MIN = 17 * 60  # 17:00
_SESSION_CLOSE_MIN = 16 * 60  # 16:00

INTRADAY_MINUTES = frozenset({1, 5, 10, 15, 30, 60, 240})
INTRADAY_SECONDS = frozenset({10, 30})


@dataclass(frozen=True)
class ResolutionSpec:
    """Normalized bar size. `seconds` is None for daily."""

    kind: str  # "seconds" | "minutes" | "daily"
    seconds: int | None

    @property
    def step_seconds(self) -> int:
        if self.kind == "daily":
            return 24 * 60 * 60
        assert self.seconds is not None
        return self.seconds


def parse_resolution(resolution: str | int) -> ResolutionSpec:
    text = str(resolution).strip().upper()
    if text in {"D", "1D"}:
        return ResolutionSpec(kind="daily", seconds=None)
    if text.endswith("S"):
        try:
            secs = int(text[:-1])
        except ValueError as exc:
            raise ValueError(f"Unsupported resolution={resolution!r}") from exc
        if secs not in INTRADAY_SECONDS:
            raise ValueError(
                f"Unsupported second resolution={resolution!r}; "
                f"supported: {sorted(INTRADAY_SECONDS)}S"
            )
        return ResolutionSpec(kind="seconds", seconds=secs)
    try:
        minutes = int(text)
    except ValueError as exc:
        raise ValueError(f"Unsupported resolution={resolution!r}") from exc
    if minutes not in INTRADAY_MINUTES:
        raise ValueError(
            f"Unsupported resolution={resolution!r}; "
            f"supported minutes={sorted(INTRADAY_MINUTES)}, "
            f"seconds={sorted(INTRADAY_SECONDS)}S, and D"
        )
    return ResolutionSpec(kind="minutes", seconds=minutes * 60)


def parse_resolution_minutes(resolution: str | int) -> int | None:
    """Back-compat: minutes for intraday minute bars, None for daily; errors on seconds."""
    spec = parse_resolution(resolution)
    if spec.kind == "daily":
        return None
    if spec.kind == "seconds":
        raise ValueError(f"Second resolution {resolution!r} has no minute size")
    assert spec.seconds is not None
    return spec.seconds // 60


def resolution_seconds(resolution: str | int) -> int:
    return parse_resolution(resolution).step_seconds


def _to_ct(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=CT)
    return ts.astimezone(CT)


def _minutes_of_day(ts: datetime) -> int:
    return ts.hour * 60 + ts.minute


def is_globex_open(ts: datetime) -> bool:
    """True if `ts` falls inside an open Globex equity-index session minute."""
    local = _to_ct(ts)
    wd = local.weekday()  # Mon=0 … Sun=6
    mins = _minutes_of_day(local)

    if wd == 5:
        return False
    if wd == 6:
        return mins >= _SESSION_OPEN_MIN
    if wd == 4:
        return mins < _SESSION_CLOSE_MIN
    if _SESSION_CLOSE_MIN <= mins < _SESSION_OPEN_MIN:
        return False
    return True


def _next_session_open(after: datetime) -> datetime:
    candidate = _to_ct(after).replace(microsecond=0)
    # Snap to whole second
    if candidate.second or candidate.microsecond:
        candidate = candidate.replace(microsecond=0) + timedelta(seconds=1)
        candidate = candidate.replace(microsecond=0)
    for _ in range(60 * 60 * 24 * 14):
        if is_globex_open(candidate):
            return candidate
        candidate += timedelta(seconds=1)
    raise RuntimeError("Unable to find next Globex session open")


def _align_at_or_after(ts: datetime, step_seconds: int) -> datetime:
    local = _to_ct(ts).replace(microsecond=0)
    epoch = int(local.timestamp())
    rem = epoch % step_seconds
    if rem == 0:
        return local
    return datetime.fromtimestamp(epoch + (step_seconds - rem), tz=local.tzinfo)


def _next_aligned_after(after: datetime, step_seconds: int) -> datetime:
    local = _to_ct(after).replace(microsecond=0)
    epoch = int(local.timestamp())
    rem = epoch % step_seconds
    if rem == 0:
        return datetime.fromtimestamp(epoch + step_seconds, tz=local.tzinfo)
    return datetime.fromtimestamp(epoch + (step_seconds - rem), tz=local.tzinfo)


def next_bar_time(after: datetime, resolution_minutes: int) -> datetime:
    """First valid Globex bar open strictly after `after` (minute resolutions)."""
    if resolution_minutes not in INTRADAY_MINUTES:
        raise ValueError(f"Unsupported resolution_minutes={resolution_minutes}")
    return _next_intraday_bar(after, resolution_minutes * 60)


def _next_intraday_bar(after: datetime, step_seconds: int) -> datetime:
    candidate = _next_aligned_after(after, step_seconds)
    for _ in range(60 * 60 * 24 * 21):
        if is_globex_open(candidate):
            return candidate
        opened = _next_session_open(candidate)
        snapped = _align_at_or_after(opened, step_seconds)
        if snapped <= candidate:
            candidate = candidate + timedelta(seconds=step_seconds)
        else:
            candidate = snapped
    raise RuntimeError("Unable to find next Globex bar within search window")


def next_daily_bar_time(after: datetime) -> datetime:
    """Next daily bar timestamp (midnight CT), skipping Saturdays."""
    day = _to_ct(after).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    for _ in range(14):
        if day.weekday() != 5:
            return day
        day += timedelta(days=1)
    raise RuntimeError("Unable to find next daily Globex bar")


def future_bar_timestamps(
    last_bar_time: datetime,
    *,
    resolution: str | int,
    count: int,
    tz: str = "America/Chicago",
) -> pd.DatetimeIndex:
    """Build `count` future bar open timestamps after `last_bar_time`."""
    if count < 1:
        raise ValueError("count must be >= 1")

    spec = parse_resolution(resolution)
    zone = ZoneInfo(tz)
    cursor = last_bar_time
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=zone)
    else:
        cursor = cursor.astimezone(zone)

    times: list[datetime] = []
    for _ in range(count):
        if spec.kind == "daily":
            cursor = next_daily_bar_time(cursor)
        else:
            assert spec.seconds is not None
            cursor = _next_intraday_bar(cursor, spec.seconds)
        times.append(cursor)
    return pd.DatetimeIndex(times)

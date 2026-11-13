from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from diviner.calendar.globex import (
    future_bar_timestamps,
    is_globex_open,
    next_bar_time,
    next_daily_bar_time,
    parse_resolution,
    resolution_seconds,
)

CT = ZoneInfo("America/Chicago")


def test_sunday_before_open_closed():
    ts = datetime(2026, 10, 25, 16, 59, tzinfo=CT)  # Sunday
    assert is_globex_open(ts) is False


def test_sunday_open():
    ts = datetime(2026, 10, 25, 17, 0, tzinfo=CT)
    assert is_globex_open(ts) is True


def test_weekday_maintenance_break():
    ts = datetime(2026, 10, 27, 16, 30, tzinfo=CT)  # Tuesday
    assert is_globex_open(ts) is False


def test_weekday_evening_open():
    ts = datetime(2026, 10, 27, 17, 0, tzinfo=CT)
    assert is_globex_open(ts) is True


def test_friday_after_close():
    ts = datetime(2026, 10, 30, 16, 0, tzinfo=CT)
    assert is_globex_open(ts) is False


def test_saturday_closed():
    ts = datetime(2026, 10, 31, 12, 0, tzinfo=CT)
    assert is_globex_open(ts) is False


def test_next_bar_skips_break():
    after = datetime(2026, 10, 27, 15, 59, tzinfo=CT)
    nxt = next_bar_time(after, 1)
    assert nxt == datetime(2026, 10, 27, 17, 0, tzinfo=CT)


def test_future_bar_timestamps_5m_count():
    last = datetime(2026, 10, 27, 10, 0, tzinfo=CT)
    idx = future_bar_timestamps(last, resolution="5", count=120)
    assert len(idx) == 120
    assert idx[0] == datetime(2026, 10, 27, 10, 5, tzinfo=CT)
    for ts in idx:
        assert is_globex_open(ts.to_pydatetime())


def test_future_bars_cross_weekend():
    last = datetime(2026, 10, 30, 15, 55, tzinfo=CT)
    idx = future_bar_timestamps(last, resolution="5", count=3)
    assert idx[0] == datetime(2026, 11, 1, 17, 0, tzinfo=CT)
    assert idx[1] == datetime(2026, 11, 1, 17, 5, tzinfo=CT)
    assert idx[2] == datetime(2026, 11, 1, 17, 10, tzinfo=CT)


def test_15m_30m_and_60m_supported():
    last = datetime(2026, 10, 27, 10, 0, tzinfo=CT)
    idx15 = future_bar_timestamps(last, resolution="15", count=4)
    assert list(idx15) == [
        datetime(2026, 10, 27, 10, 15, tzinfo=CT),
        datetime(2026, 10, 27, 10, 30, tzinfo=CT),
        datetime(2026, 10, 27, 10, 45, tzinfo=CT),
        datetime(2026, 10, 27, 11, 0, tzinfo=CT),
    ]
    idx30 = future_bar_timestamps(last, resolution="30", count=2)
    assert idx30[0] == datetime(2026, 10, 27, 10, 30, tzinfo=CT)
    assert idx30[1] == datetime(2026, 10, 27, 11, 0, tzinfo=CT)
    idx60 = future_bar_timestamps(last, resolution="60", count=2)
    assert idx60[0] == datetime(2026, 10, 27, 11, 0, tzinfo=CT)
    assert idx60[1] == datetime(2026, 10, 27, 12, 0, tzinfo=CT)


def test_10s_resolution():
    last = datetime(2026, 10, 27, 10, 0, 0, tzinfo=CT)
    idx = future_bar_timestamps(last, resolution="10S", count=3)
    assert idx[0] == datetime(2026, 10, 27, 10, 0, 10, tzinfo=CT)
    assert idx[1] == datetime(2026, 10, 27, 10, 0, 20, tzinfo=CT)
    assert idx[2] == datetime(2026, 10, 27, 10, 0, 30, tzinfo=CT)


def test_4h_skips_closed_to_session():
    last = datetime(2026, 10, 30, 15, 0, tzinfo=CT)
    idx = future_bar_timestamps(last, resolution="240", count=1)
    assert is_globex_open(idx[0].to_pydatetime())
    assert idx[0] >= datetime(2026, 11, 1, 17, 0, tzinfo=CT)


def test_daily_skips_saturday():
    last = datetime(2026, 10, 30, 12, 0, tzinfo=CT)
    nxt = next_daily_bar_time(last)
    assert nxt == datetime(2026, 11, 1, 0, 0, tzinfo=CT)
    idx = future_bar_timestamps(last, resolution="D", count=2)
    assert idx[0] == datetime(2026, 11, 1, 0, 0, tzinfo=CT)
    assert idx[1] == datetime(2026, 11, 2, 0, 0, tzinfo=CT)


def test_parse_resolution():
    assert parse_resolution("15").seconds == 900
    assert parse_resolution("10S").seconds == 10
    assert parse_resolution("D").kind == "daily"
    assert resolution_seconds("60") == 3600
    assert resolution_seconds("30S") == 30
    assert resolution_seconds("1D") == 86400


def test_unsupported_resolution():
    last = datetime(2026, 10, 27, 10, 0, tzinfo=CT)
    with pytest.raises(ValueError):
        future_bar_timestamps(last, resolution="7", count=10)
    with pytest.raises(ValueError):
        future_bar_timestamps(last, resolution="2", count=10)

from diviner.tv.client import TvClient
from diviner.tv.overlay import forecast_to_close_points


def test_forecast_to_close_points():
    rows = [
        {"time": 100, "close": 1.5, "open": 1.0},
        {"time": 200, "close": 2.0, "open": 1.5},
    ]
    pts = forecast_to_close_points(rows)
    assert pts == [{"time": 100, "price": 1.5}, {"time": 200, "price": 2.0}]


def test_parse_polyline_stdout_last_json_line():
    raw = 'noise\n{"success": true, "segments": 3, "entity_ids": ["a"]}\n'
    parsed = TvClient._parse_polyline_stdout(raw)
    assert parsed["success"] is True
    assert parsed["segments"] == 3

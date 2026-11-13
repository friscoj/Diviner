#!/usr/bin/env python3
"""Smoke-test TradingView CLI fetch for ES/NQ at 1m and 5m."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diviner.config import load_settings
from diviner.tv.client import TvClient, TvError


def main() -> int:
    settings = load_settings()
    client = TvClient(settings)
    print("TV CLI:", settings.tv_node, settings.tv_cli)
    try:
        status = client.status()
    except TvError as exc:
        print("FAIL status:", exc)
        return 2
    print("status:", status)
    if not (status.get("success") and status.get("cdp_connected")):
        print("FAIL: CDP not connected. Launch TradingView with --remote-debugging-port=9222")
        return 2

    for symbol in settings.symbol_suggestions:
        for tf in ("1", "5"):
            print(f"\n=== {symbol} @ {tf} ===")
            bars = client.fetch_lookback(symbol, tf, settings.lookback)
            print(f"bars={len(bars)} first={bars[0].time} last={bars[-1].time}")
            print(f"last close={bars[-1].close} volume={bars[-1].volume}")
            if len(bars) < settings.lookback:
                print(f"WARN: fewer than lookback={settings.lookback}")
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

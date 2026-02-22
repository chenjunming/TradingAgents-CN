from __future__ import annotations

from app.services.signals.indicators import build_indicator_context


def test_build_indicator_context_basics():
    kline = []
    for i in range(1, 80):
        kline.append({"time": f"2026-01-{i:02d}", "close": 100 + i * 0.5, "volume": 100000 + i * 1000})

    ctx = build_indicator_context(kline)

    assert ctx["values"]["close"] is not None
    assert ctx["values"]["ma20"] is not None
    assert ctx["values"]["ma50"] is not None
    assert ctx["values"]["rsi14"] is not None

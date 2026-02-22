from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window) / float(period)


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += abs(delta)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def vwap(closes: Sequence[float], volumes: Sequence[float], period: int = 20) -> Optional[float]:
    if len(closes) < period or len(volumes) < period:
        return None
    c = closes[-period:]
    v = volumes[-period:]
    total_vol = sum(v)
    if total_vol <= 0:
        return None
    return sum(px * vol for px, vol in zip(c, v)) / total_vol


def volume_ratio(volumes: Sequence[float], period: int = 20) -> Optional[float]:
    if len(volumes) < period + 1:
        return None
    current = volumes[-1]
    base = volumes[-(period + 1):-1]
    avg = sum(base) / float(period)
    if avg <= 0:
        return None
    return current / avg


def trend_slope(values: Sequence[float], window: int = 5) -> Optional[float]:
    if window <= 1 or len(values) < window:
        return None
    y = list(values[-window:])
    x = list(range(window))
    x_mean = sum(x) / window
    y_mean = sum(y) / window
    num = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
    den = sum((xi - x_mean) ** 2 for xi in x)
    if den == 0:
        return None
    return num / den


def build_indicator_context(kline: List[Dict[str, Any]], current_price: Optional[float] = None) -> Dict[str, Any]:
    sorted_items = sorted(kline, key=lambda x: str(x.get("time") or x.get("trade_date") or ""))
    closes = [_to_float(x.get("close")) for x in sorted_items]
    closes = [x for x in closes if x is not None]
    volumes = [_to_float(x.get("volume") or x.get("vol")) for x in sorted_items]
    volumes = [x if x is not None else 0.0 for x in volumes][-len(closes):]

    if not closes:
        return {"values": {}, "prev_values": {}, "series": {"close": []}}

    close_now = float(current_price) if current_price is not None else closes[-1]
    close_prev = closes[-2] if len(closes) > 1 else None

    close_series = closes[:-1] + [close_now]
    ma20 = sma(close_series, 20)
    ma50 = sma(close_series, 50)
    ma100 = sma(close_series, 100)

    prev_ma20 = sma(closes[:-1], 20)
    prev_ma50 = sma(closes[:-1], 50)
    prev_ma100 = sma(closes[:-1], 100)

    rsi14 = rsi(close_series, 14)
    prev_rsi14 = rsi(closes[:-1], 14)

    vw = vwap(close_series, volumes, 20)
    prev_vw = vwap(closes[:-1], volumes[:-1], 20) if len(volumes) > 1 else None

    vr = volume_ratio(volumes, 20)
    prev_vr = volume_ratio(volumes[:-1], 20) if len(volumes) > 1 else None

    ma50_series = []
    for i in range(50, len(close_series) + 1):
        ma50_series.append(sum(close_series[i - 50:i]) / 50.0)

    return {
        "values": {
            "close": close_now,
            "ma20": ma20,
            "ma50": ma50,
            "ma100": ma100,
            "rsi14": rsi14,
            "vwap": vw,
            "vol_ratio_20d": vr,
        },
        "prev_values": {
            "close": close_prev,
            "ma20": prev_ma20,
            "ma50": prev_ma50,
            "ma100": prev_ma100,
            "rsi14": prev_rsi14,
            "vwap": prev_vw,
            "vol_ratio_20d": prev_vr,
        },
        "series": {
            "close": close_series,
            "volume": volumes,
            "ma50": ma50_series,
            "ma20": [],
            "ma100": [],
        },
        "slope": {
            "ma50": trend_slope(ma50_series, 5) if ma50_series else None,
            "close": trend_slope(close_series, 5),
        },
    }

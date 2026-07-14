"""
Chart-structure features derived from OHLC.

Everything here is computed from price highs and lows -- the one technical input
the rest of the scan ignores. EMA stacks, RSI and MACD are all transforms of the
close series, so they tend to fire and fail together; swing structure is derived
from the extremes and can therefore confirm (or veto) them independently. A
ticker with a perfect EMA stack that is carving lower highs into resistance is
distributing, and nothing else in the payload can see that.

Two things are produced per timeframe:

  * SWING SEQUENCE -- fractal pivot highs/lows, read as higher-highs /
    higher-lows (uptrend), lower-highs / lower-lows (downtrend), or neither.
  * CONSOLIDATION -- the longest recent window whose high/low range stays inside
    a width threshold, giving a range box, its duration, and how far price sits
    below the range high.

The nearest pivot below price is structural support (a stop anchor) and the
nearest pivot above is structural resistance (a target anchor). These are what
the prompt's stop/target rules have always asked for and never actually had.

Pure pandas/numpy, no network I/O. Called from data.fmp with frames it has
already fetched, so this adds no API calls.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger("flowscanner.structure")


def _round(value: Any, digits: int = 2) -> Optional[float]:
    try:
        if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Pivots
# --------------------------------------------------------------------------- #
def _pivots(df: pd.DataFrame, width: int) -> tuple[list[float], list[float]]:
    """
    Fractal pivots: a bar is a pivot high if its high is the strict max of the
    `width` bars either side of it (mirror for pivot lows). Returns
    (pivot_highs, pivot_lows) as price lists in chronological order.

    The last `width` bars can never confirm a pivot -- that lag is inherent to
    swing structure, not a bug: an unconfirmed high is not yet a swing high.
    """
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    n = len(df)

    pivot_highs: list[float] = []
    pivot_lows: list[float] = []
    for i in range(width, n - width):
        window_h = highs[i - width : i + width + 1]
        window_l = lows[i - width : i + width + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            pivot_highs.append(float(highs[i]))
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            pivot_lows.append(float(lows[i]))
    return pivot_highs, pivot_lows


def _sequence(pivots: list[float]) -> tuple[bool, bool]:
    """(rising, falling) for the last two pivots of a series."""
    if len(pivots) < 2:
        return False, False
    return pivots[-1] > pivots[-2], pivots[-1] < pivots[-2]


# --------------------------------------------------------------------------- #
# Consolidation
# --------------------------------------------------------------------------- #
def _consolidation(
    df: pd.DataFrame, max_width_pct: float, min_bars: int
) -> dict:
    """
    Walk backwards from the last bar, extending a running high/low, and keep
    going while the range stays inside `max_width_pct`. The number of bars
    survived IS the range duration -- a range is simply the longest recent window
    price has not escaped, so this needs no fixed window guess.
    """
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    n = len(df)
    if n < min_bars:
        return {"in_consolidation": False}

    run_high = highs[-1]
    run_low = lows[-1]
    bars = 1
    for i in range(n - 2, -1, -1):
        cand_high = max(run_high, highs[i])
        cand_low = min(run_low, lows[i])
        if cand_low <= 0:
            break
        if (cand_high / cand_low - 1.0) * 100.0 > max_width_pct:
            break
        run_high, run_low, bars = cand_high, cand_low, bars + 1

    if bars < min_bars:
        # Too short to be a base. Emit nothing but the flag: a 1-bar "range" is
        # noise, and handing it over as a range box invites it to be traded as one.
        return {"in_consolidation": False}

    width_pct = (run_high / run_low - 1.0) * 100.0 if run_low > 0 else None
    return {
        "in_consolidation": True,
        "range_high": _round(run_high),
        "range_low": _round(run_low),
        "range_width_pct": _round(width_pct),
        "bars_in_range": int(bars),
    }


# --------------------------------------------------------------------------- #
# Per-timeframe analysis
# --------------------------------------------------------------------------- #
def analyze_timeframe(
    df: pd.DataFrame,
    *,
    pivot_width: int,
    lookback: int,
    max_range_width_pct: float,
    min_range_bars: int,
) -> dict:
    """
    Structure features for one OHLC frame. `df` needs high/low/close columns and
    must be sorted oldest -> newest; the index is ignored.
    """
    if df is None or len(df) < pivot_width * 2 + 2:
        return {"available": False}

    window = df.tail(lookback)
    price = float(window["close"].iloc[-1])
    if price <= 0:
        return {"available": False}

    pivot_highs, pivot_lows = _pivots(window, pivot_width)
    hh, lh = _sequence(pivot_highs)
    hl, ll = _sequence(pivot_lows)

    cons = _consolidation(window, max_range_width_pct, min_range_bars)
    in_range = bool(cons.get("in_consolidation"))

    # Price outranks a stale pivot sequence. A pivot needs `pivot_width` bars to
    # confirm, so the last confirmed pivot can already be overtaken by price: a
    # stock trading above its last pivot high has *broken* the lower-high
    # sequence, and calling that a downtrend would flag a breakout as
    # distribution. Same in reverse for a breakdown through the last pivot low.
    broke_out = bool(pivot_highs) and price > pivot_highs[-1]
    broke_down = bool(pivot_lows) and price < pivot_lows[-1]

    if hh and hl and not broke_down:
        trend = "uptrend"
    elif lh and ll and not broke_out:
        trend = "downtrend"
    elif in_range:
        trend = "range"
    else:
        trend = "choppy"

    # Nearest confirmed pivot either side of price: the stop and target anchors.
    below = [p for p in pivot_lows if p < price]
    above = [p for p in pivot_highs if p > price]
    support = max(below) if below else None
    resistance = min(above) if above else None

    result = {
        "available": True,
        "trend": trend,
        "higher_highs": hh,
        "higher_lows": hl,
        "lower_highs": lh,
        "lower_lows": ll,
        "recent_pivot_highs": [_round(p) for p in pivot_highs[-3:]],
        "recent_pivot_lows": [_round(p) for p in pivot_lows[-3:]],
        "price_above_last_pivot_high": broke_out,
        "price_below_last_pivot_low": broke_down,
        "nearest_support": _round(support),
        "dist_to_support_pct": _round((price / support - 1.0) * 100.0) if support else None,
        "nearest_resistance": _round(resistance),
        "dist_to_resistance_pct": (
            _round((resistance / price - 1.0) * 100.0) if resistance else None
        ),
        # No confirmed pivot high above price means price is at the highs of the
        # lookback -- a breakout, not a failure to find resistance.
        "at_lookback_highs": resistance is None,
    }
    result.update(cons)
    if in_range and cons.get("range_high"):
        result["dist_to_range_high_pct"] = _round(
            (float(cons["range_high"]) / price - 1.0) * 100.0
        )
    return result


def build(daily: pd.DataFrame, weekly: pd.DataFrame) -> dict:
    """
    Daily + weekly structure for one symbol. Weekly comes free: the frame is
    already derived from the same daily bars, and a daily pullback inside a
    weekly uptrend is exactly the setup this scanner exists to find.

    Never raises -- structure is additive context, so a failure degrades to
    {"available": False} rather than dropping the ticker from the scan.
    """
    out: dict = {}
    for name, frame, width, lookback, max_pct, min_bars in (
        (
            "daily",
            daily,
            config.PIVOT_WIDTH_DAILY,
            config.STRUCTURE_LOOKBACK_DAILY,
            config.CONSOLIDATION_MAX_WIDTH_PCT_DAILY,
            config.CONSOLIDATION_MIN_BARS_DAILY,
        ),
        (
            "weekly",
            weekly,
            config.PIVOT_WIDTH_WEEKLY,
            config.STRUCTURE_LOOKBACK_WEEKLY,
            config.CONSOLIDATION_MAX_WIDTH_PCT_WEEKLY,
            config.CONSOLIDATION_MIN_BARS_WEEKLY,
        ),
    ):
        try:
            out[name] = analyze_timeframe(
                frame,
                pivot_width=width,
                lookback=lookback,
                max_range_width_pct=max_pct,
                min_range_bars=min_bars,
            )
        except Exception as exc:  # defensive: structure must never kill a ticker
            logger.warning("Structure analysis failed on %s frame: %s", name, exc)
            out[name] = {"available": False}
    return out

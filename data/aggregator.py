"""
Aggregator: combine FMP, Unusual Whales, earnings, macro and VIX data into a
single structured payload per scan session.

This module performs no network I/O. It takes already-fetched per-source data
(from data.fmp / data.unusual_whales / data.vix) and produces the compact,
analysis-ready structure that is sent to Claude in one payload.

Relative strength (5d / 20d vs SPY) is computed here because it requires both
the ticker's returns and SPY's returns together.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

logger = logging.getLogger("flowscanner.aggregator")


def _days_until(date_iso: Optional[str]) -> Optional[int]:
    if not date_iso:
        return None
    try:
        d = dt.date.fromisoformat(date_iso[:10])
        return (d - dt.date.today()).days
    except ValueError:
        return None


def build_macro_context(
    macro_fmp: dict[str, Optional[dict]], vix: dict
) -> dict:
    """
    Build the macro block (SPY/QQQ trend + VIX) shown in the header and given to
    Claude for macro-alignment scoring.
    """
    macro: dict = {"vix": vix}
    for sym in ("SPY", "QQQ"):
        data = macro_fmp.get(sym)
        if not data:
            macro[sym.lower()] = {"available": False}
            continue
        d = data["daily"]
        close = d.get("close")
        ema50 = d.get("ema50")
        ema200 = d.get("ema200")
        trend = "neutral"
        if close and ema50 and ema200:
            if close > ema50 > ema200:
                trend = "bullish"
            elif close < ema50 < ema200:
                trend = "bearish"
            elif close > ema200:
                trend = "mildly bullish"
            else:
                trend = "mildly bearish"
        macro[sym.lower()] = {
            "available": True,
            "close": close,
            "rsi14": d.get("rsi14"),
            "ema50": ema50,
            "ema200": ema200,
            "above_ema200": d.get("price_above_ema200"),
            "trend": trend,
            "ret5": data["returns"].get("ret5"),
            "ret20": data["returns"].get("ret20"),
        }
    return macro


def build_ticker_record(
    fmp_data: dict,
    uw_data: dict,
    earnings_date: Optional[str],
    spy_returns: dict,
    screener_row: Optional[dict] = None,
) -> dict:
    """Merge one ticker's sources into a single compact record."""
    symbol = fmp_data["symbol"]

    ret5 = fmp_data["returns"].get("ret5")
    ret20 = fmp_data["returns"].get("ret20")
    spy5 = spy_returns.get("ret5")
    spy20 = spy_returns.get("ret20")

    rs = {
        "ret5": ret5,
        "ret20": ret20,
        "spy_ret5": spy5,
        "spy_ret20": spy20,
        "ret5_vs_spy": round(ret5 - spy5, 2) if (ret5 is not None and spy5 is not None) else None,
        "ret20_vs_spy": round(ret20 - spy20, 2)
        if (ret20 is not None and spy20 is not None)
        else None,
    }

    record = {
        "symbol": symbol,
        "price": fmp_data["price"],
        "market_cap": (screener_row or {}).get("marketCap"),
        "sector": (screener_row or {}).get("sector"),
        "avg_volume": (screener_row or {}).get("volume"),
        "daily": fmp_data["daily"],
        "weekly": fmp_data["weekly"],
        "hourly": fmp_data["hourly"],
        "relative_strength": rs,
        "options": {
            "iv": uw_data.get("iv"),
            "iv_rank": uw_data.get("iv_rank"),
            "put_call_ratio": uw_data.get("put_call_ratio"),
            "top_oi": uw_data.get("top_oi", []),
            "flow_alerts": uw_data.get("flow_alerts", []),
            "flow_count": uw_data.get("flow_count", 0),
            "total_bull_premium": uw_data.get("total_bull_premium", 0.0),
        },
        "earnings": {
            "next_date": earnings_date,
            "days_to_earnings": _days_until(earnings_date),
        },
    }
    return record


def assemble_payload(
    session_name: str,
    ticker_records: list[dict],
    macro: dict,
) -> dict:
    """Final structured payload sent to Claude for one scan session."""
    return {
        "scan_session": session_name,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "universe_size": len(ticker_records),
        "macro": macro,
        "tickers": ticker_records,
    }

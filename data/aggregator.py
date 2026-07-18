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
import statistics
from typing import Optional

import config
from data import sector_etf

logger = logging.getLogger("flowscanner.aggregator")


def _pct_true(values: list[Optional[bool]]) -> Optional[float]:
    """
    Percentage of KNOWN values that are True.

    None is not False. data/fmp.py deliberately emits None for "this ticker has no
    EMA200 yet" so that unknown stays distinguishable from bearish; counting those
    Nones in the denominator would silently drag breadth down every time the rotate
    list picks up a recent listing. Unknown ticker -> excluded, not counted against.
    """
    known = [v for v in values if v is not None]
    if not known:
        return None
    return round(sum(1 for v in known if v) / len(known) * 100.0, 1)


def _median(values: list[Optional[float]]) -> Optional[float]:
    known = [v for v in values if v is not None]
    if not known:
        return None
    return round(statistics.median(known), 2)


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


def build_breadth(records: list[dict]) -> dict:
    """
    What the WHOLE scanned universe is doing -- the scanner's only true breadth.

    Costs nothing: `price_above_ema200` and `ema_stack_bullish` are already computed
    for all ~50-60 names every session and were simply thrown away. Two SPY chips
    cannot tell a mega-cap-led tape from a broad one; 60 names can.
    """
    if not records:
        return {"available": False}

    dailies = [r.get("daily") or {} for r in records]
    ret_from_open = [d.get("ret_from_open_pct") for d in dailies]

    # Is this today's session or yesterday's close? Decide by MAJORITY, never on one
    # ticker: a single stale or halted name must not flip the label for the page.
    flags = [d.get("bar_is_today") for d in dailies if d.get("bar_is_today") is not None]
    intraday = bool(flags) and sum(1 for f in flags if f) > len(flags) / 2

    return {
        "available": True,
        "universe_size": len(records),
        "pct_above_ema200": _pct_true([d.get("price_above_ema200") for d in dailies]),
        "pct_ema_stack_bullish": _pct_true([d.get("ema_stack_bullish") for d in dailies]),
        "pct_green": _pct_true([(v > 0) if v is not None else None for v in ret_from_open]),
        "median_ret_from_open_pct": _median(ret_from_open),
        "median_range_position_pct": _median([d.get("range_position_pct") for d in dailies]),
        "as_of": "intraday" if intraday else "prior_close",
    }


def _classify_tape(spy_ret_from_open: Optional[float], pct_green: Optional[float]) -> str:
    """
    One deterministic label for the tape. Computed here, in Python, for the same
    reason every other number on the card is: so it cannot drift with the prose.

    The extreme labels need BOTH the index move and participation to agree. SPY down
    0.9% on a mega-cap unwind while 60% of the universe is green is not a selloff,
    and calling it one would veto perfectly good setups.
    """
    if spy_ret_from_open is None:
        return "unknown"
    if (
        spy_ret_from_open <= config.TAPE_SELLOFF_SPY_PCT
        and pct_green is not None
        and pct_green < config.TAPE_SELLOFF_BREADTH_PCT
    ):
        return "broad_selloff"
    if (
        spy_ret_from_open >= config.TAPE_RALLY_SPY_PCT
        and pct_green is not None
        and pct_green > config.TAPE_RALLY_BREADTH_PCT
    ):
        return "broad_rally"
    if spy_ret_from_open < config.TAPE_SOFT_SPY_PCT:
        return "soft"
    if spy_ret_from_open > config.TAPE_FIRM_SPY_PCT:
        return "firm"
    return "mixed"


def _index_progress(data: Optional[dict]) -> dict:
    """Day-progression numbers for one index, or {"available": False}."""
    if not data:
        return {"available": False}
    d = data.get("daily") or {}
    h = data.get("hourly") or {}
    close = d.get("close")
    svwap = h.get("session_vwap")
    return {
        "available": True,
        "bar_is_today": d.get("bar_is_today"),
        "ret_from_open_pct": d.get("ret_from_open_pct"),
        "range_position_pct": d.get("range_position_pct"),
        "vs_session_vwap_pct": (
            round((close / svwap - 1.0) * 100.0, 2)
            if close and svwap and svwap > 0
            else None
        ),
    }


def build_day_progress(
    macro_fmp: dict[str, Optional[dict]], breadth: dict, vix: dict
) -> dict:
    """
    How the session is actually trading -- the context a card needs before it says
    "buy this now".

    THE HONEST PART: the premarket scan runs at 09:00 ET. There is no day to report
    on yet, and the correct answer is to say so rather than to dress yesterday's bar
    up as today's tape. So this reports `as_of: "premarket"`, `tape: "unknown"`, and
    hands over the PRIOR session instead -- "yesterday closed at 9% of its range with
    22% of the universe green" is genuinely predictive of the open and is the best
    thing that can truthfully be said before the bell.

    Which branch applies is decided by the DATA (bar_is_today), never by the session
    name: --force, a manual run or a late dispatch all detach the label from reality.
    """
    spy = _index_progress(macro_fmp.get("SPY"))
    qqq = _index_progress(macro_fmp.get("QQQ"))

    live = spy.get("bar_is_today") is True and breadth.get("as_of") == "intraday"
    spy_ret = spy.get("ret_from_open_pct")
    pct_green = breadth.get("pct_green")

    out = {
        "as_of": "intraday" if live else "premarket",
        "spy": spy,
        "qqq": qqq,
        "vix_change_pct": (vix or {}).get("change_pct"),
        "breadth": breadth,
    }

    if live:
        out["tape"] = _classify_tape(spy_ret, pct_green)
        return out

    # Before the open: the newest daily bar IS the prior session. Same numbers, a
    # different meaning -- so give them a different name rather than a tape label.
    out["tape"] = "unknown"
    out["prior_session"] = {
        "spy_ret_from_open_pct": spy_ret,
        "spy_range_position_pct": spy.get("range_position_pct"),
        "pct_green": pct_green,
        "median_range_position_pct": breadth.get("median_range_position_pct"),
    }
    return out


def build_ticker_record(
    fmp_data: dict,
    uw_data: dict,
    earnings_date: Optional[str],
    spy_returns: dict,
    screener_row: Optional[dict] = None,
    sector_etf_symbol: Optional[str] = None,
    sector_etf_data: Optional[dict] = None,
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
        "structure": fmp_data.get("structure", {}),
        "entry_zone": fmp_data.get("entry_zone", {}),
        "hourly": fmp_data["hourly"],
        "relative_strength": rs,
        # How the ticker's own sector ETF is trading -- context for the caution layer.
        # A bullish name inside a rolling-over sector is a real setup with a real
        # headwind; `stock_diverging_from_weak_sector` is the flag that says so.
        "sector_context": sector_etf.build_sector_context(
            etf_symbol=sector_etf_symbol,
            etf_data=sector_etf_data,
            spy_returns=spy_returns,
            stock_daily=fmp_data["daily"],
        ),
        "options": {
            "iv": uw_data.get("iv"),
            "iv_rank": uw_data.get("iv_rank"),
            "put_call_ratio": uw_data.get("put_call_ratio"),
            "bullish_premium": uw_data.get("bullish_premium"),
            "bearish_premium": uw_data.get("bearish_premium"),
            "net_call_premium_day": uw_data.get("net_call_premium_day"),
            "net_put_premium_day": uw_data.get("net_put_premium_day"),
            "top_oi": uw_data.get("top_oi", []),
            # Real, listed calls the model must choose from (strike/expiry/bid/ask/
            # IV/OI/delta all from the live chain) -- see data/unusual_whales.py.
            "call_candidates": uw_data.get("call_candidates", []),
            # Flow, split by actual direction (calls BOUGHT vs SOLD).
            "flow_available": uw_data.get("flow_available", False),
            "flow_alerts": uw_data.get("flow_alerts", []),
            "flow_count": uw_data.get("flow_count", 0),
            "bullish_alert_count": uw_data.get("bullish_alert_count", 0),
            "bearish_alert_count": uw_data.get("bearish_alert_count", 0),
            "bought_call_premium": uw_data.get("bought_call_premium", 0.0),
            "sold_call_premium": uw_data.get("sold_call_premium", 0.0),
            "bought_put_premium": uw_data.get("bought_put_premium", 0.0),
            "net_call_premium": uw_data.get("net_call_premium", 0.0),
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

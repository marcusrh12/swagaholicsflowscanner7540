"""
Sector-ETF context: map each scanned ticker to the ETF that tracks its sector,
and turn that ETF's price action into a compact "is the whole group healthy?"
read attached to every ticker record.

The point is a caution layer, not a new confluence: a bullish chart inside a
sinking sector is a real setup carrying a real headwind, and the card should say
so. This module performs NO network I/O -- it only maps sectors to symbols and
derives the context block from ETF data that main.py has already fetched (via the
same FMPClient.get_ticker_data path used for SPY/QQQ).
"""

from __future__ import annotations

from typing import Optional

# Sector -> tracking ETF. Keyed on FMP's own sector taxonomy ("Financial Services",
# "Consumer Cyclical", ...) with the friendlier names from the spec kept as aliases
# so the mapping survives either vocabulary. Semiconductors are handled separately
# (industry-level) because they move on their own cycle, not with broad tech.
SECTOR_ETF_MAP: dict[str, str] = {
    # FMP taxonomy
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    # Spec / colloquial aliases
    "Financials": "XLF",
    "Financial": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Materials": "XLB",
    "Communication": "XLC",
    "Communications": "XLC",
    "Health Care": "XLV",
}

# Semiconductor names track SMH, not XLK. Matched on the industry string (FMP uses
# "Semiconductors" and "Semiconductor Equipment & Materials").
_SEMI_ETF = "SMH"


def etf_for(sector: Optional[str], industry: Optional[str]) -> Optional[str]:
    """
    Pick the sector ETF for one ticker from its FMP sector/industry classification.
    Semiconductor industries prefer SMH over the broad-tech XLK. Returns None when
    the sector is unknown or unmapped (a name we simply have no group read for).
    """
    if industry and "semiconductor" in industry.lower():
        return _SEMI_ETF
    if not sector:
        return None
    return SECTOR_ETF_MAP.get(sector.strip())


def all_etfs() -> set[str]:
    """Every symbol this module can ask for -- handy for pre-fetching if desired."""
    return set(SECTOR_ETF_MAP.values()) | {_SEMI_ETF}


def _etf_trend(above21: Optional[bool], above50: Optional[bool]) -> Optional[str]:
    """
    Coarse trend state from the ETF's own 21/50 EMA. Above both = uptrend, below
    both = downtrend, disagreement = mixed. None when either EMA is unavailable so
    "unknown" never masquerades as "downtrend".
    """
    if above21 is None or above50 is None:
        return None
    if above21 and above50:
        return "uptrend"
    if not above21 and not above50:
        return "downtrend"
    return "mixed"


def sector_improving(
    etf_data: Optional[dict], spy_returns: dict
) -> tuple[bool, float]:
    """
    Is this sector STARTING TO TURN UP? Returns (is_improving, accel_score).

    "Turning up" is deliberately EARLIER than "is a leader": short-term momentum
    positive AND accelerating versus the trailing 20-day pace. It does NOT require the
    ETF to already be beating SPY (`ret20_vs_spy` positive), so it catches a group while
    it is still technically weak on a 20-day basis but has just started to move -- the
    "rotation out of X into Y" case, before Y shows up as a leader.

      * `ret5 > 0`              -- the group is actually up over the last week.
      * `accel = ret5 - ret20/4 > 0` -- the recent 5-day pace exceeds the trailing
        20-day average per-5-day pace (ret20/4 ≈ that pace), i.e. it is accelerating,
        not just drifting.

    `accel` doubles as a ranking score so callers can pick the STRONGEST-improving
    sectors. Missing ETF data or returns yields (False, 0.0) -- an unverifiable sector
    never qualifies, matching build_sector_context's defensive posture. `spy_returns`
    is accepted for signature symmetry with build_sector_context and future SPY-relative
    tuning; the current definition is intentionally SPY-independent (see above).
    """
    rets = (etf_data or {}).get("returns") or {}
    ret5 = rets.get("ret5")
    ret20 = rets.get("ret20")
    if ret5 is None or ret20 is None:
        return (False, 0.0)
    accel = ret5 - (ret20 / 4.0)
    return (ret5 > 0 and accel > 0, round(accel, 3))


def build_sector_context(
    etf_symbol: Optional[str],
    etf_data: Optional[dict],
    spy_returns: dict,
    stock_daily: dict,
) -> dict:
    """
    Build the `sector_context` block for one ticker.

    Inputs:
      * etf_symbol / etf_data -- the sector ETF and its full get_ticker_data payload.
      * spy_returns           -- SPY's {ret5, ret20}, for the ETF-vs-SPY relative read.
      * stock_daily           -- the ticker's own `daily` block, to judge whether it is
                                 bullish while its sector is not (the divergence flag).

    Returns a dict that is always safe to serialize; `available: False` when there is
    no ETF match or no ETF data (a recent listing, a fetch miss, an unmapped sector).
    """
    if not etf_symbol or not etf_data:
        return {"available": False, "etf": etf_symbol}

    d = etf_data.get("daily") or {}
    rets = etf_data.get("returns") or {}
    close = d.get("close")
    ema21 = d.get("ema21")
    ema50 = d.get("ema50")

    above21 = (close > ema21) if (close is not None and ema21 is not None) else None
    above50 = (close > ema50) if (close is not None and ema50 is not None) else None
    trend = _etf_trend(above21, above50)

    ret5 = rets.get("ret5")
    ret20 = rets.get("ret20")
    spy20 = spy_returns.get("ret20")
    ret20_vs_spy = (
        round(ret20 - spy20, 2)
        if (ret20 is not None and spy20 is not None)
        else None
    )

    downtrend = trend == "downtrend"
    underperforming_spy = ret20_vs_spy is not None and ret20_vs_spy < 0
    # A "weak sector" is one the setup is fighting: either the ETF itself is rolling
    # over, or it is lagging the market it belongs to over the last 20 sessions.
    sector_weak = downtrend or underperforming_spy

    # Is the ticker itself bullish? Read from the same EMA structure everything else
    # is scored on. A green chart inside a weak group is exactly the divergence the
    # caution layer exists to surface.
    stock_bullish = bool(stock_daily.get("ema_stack_bullish")) or bool(
        stock_daily.get("price_above_ema200")
    )

    return {
        "available": True,
        "etf": etf_symbol,
        "trend": trend,
        "above_ema21": above21,
        "above_ema50": above50,
        "ret5": ret5,
        "ret20": ret20,
        "ret20_vs_spy": ret20_vs_spy,
        "downtrend": downtrend,
        "underperforming_spy": underperforming_spy,
        "sector_weak": sector_weak,
        "stock_diverging_from_weak_sector": bool(stock_bullish and sector_weak),
    }

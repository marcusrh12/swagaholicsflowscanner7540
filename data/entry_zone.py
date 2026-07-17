"""
Entry zones: where a setup is worth buying, not merely that it exists.

The card's stop and target have always been anchored to real structure. Entry never
was -- `entry_reference` was just the last print, so a name 4% extended above its
support and a name sitting right on it produced identical-looking cards. The whole
difference between those two is the trade.

A zone is a BAND, built by clustering independent support levels that agree on a
price, scored by:

  * WHAT AGREES -- a confirmed pivot low (where the market actually turned) outranks
    an EMA (a curve that happens to pass nearby). Weights live in config.ZONE_WEIGHTS.
  * HOW INDEPENDENTLY -- the score dedupes by level FAMILY, so EMA8/21/50 stacking at
    one price counts once. Three views of the close series agreeing is one opinion,
    not three.
  * WHETHER IT HAS EVER HELD -- `_touch_count` counts bars that traded INTO the band
    and closed back above it. That is the literal question ("can it act as support?")
    answered with evidence rather than theory, and nothing else in the scanner
    measures it.

`status` is the part that answers the original complaint: `extended` means price is
above the zone and you would be chasing; `in_zone` means it is here now.

Deliberately NOT a gate. A breakout at new highs has no pivot overhead and often
nothing meaningful below -- it scores `weak`, and those are exactly the momentum
names this scanner exists to find. The zone informs the entry; it must never veto
the setup. See claude_engine for what actually gates.

Pure pandas/numpy, no network I/O. Fed frames data.fmp already holds, so it adds no
API calls. Never raises -- degrades to {"available": False}.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger("flowscanner.entry_zone")


def _round(value: Any, digits: int = 2) -> Optional[float]:
    try:
        if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    """A finite, positive price or nothing. Levels at <= 0 are not levels."""
    try:
        if value is None:
            return None
        f = float(value)
        if not np.isfinite(f) or f <= 0:
            return None
        return f
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Levels
# --------------------------------------------------------------------------- #
def _collect_levels(
    *,
    price: float,
    atr14: float,
    daily: pd.DataFrame,
    structure: dict,
    emas: dict,
    weekly_ema21: Optional[float],
    avwap_10d: Optional[float],
    session_vwap: Optional[float],
) -> list[dict]:
    """
    Candidate support levels within a plausible pullback window, each tagged with
    the family it belongs to (for dedupe) and a weight (for scoring).

    The window is [price - ZONE_MAX_DEPTH_ATR*atr, price * 1.005]. The upper bound
    is deliberately ABOVE price: price may already be sitting inside its zone, and a
    strict `level < price` filter would make the in-zone case unreachable -- the
    band would always be clipped to start below the current print.
    """
    lo = price - config.ZONE_MAX_DEPTH_ATR * atr14
    hi = price * 1.005

    d = structure.get("daily") or {}
    w = structure.get("weekly") or {}

    raw: list[tuple[Optional[float], str, str, float]] = []

    # Pivots -- where the market actually turned.
    for p in (d.get("recent_pivot_lows") or []):
        raw.append((_num(p), "pivot", "Daily pivot low", config.ZONE_WEIGHTS["pivot"]))
    raw.append(
        (_num(d.get("nearest_support")), "pivot", "Daily support", config.ZONE_WEIGHTS["pivot"])
    )
    raw.append(
        (_num(w.get("nearest_support")), "pivot", "Weekly support", config.ZONE_WEIGHTS["pivot"])
    )

    # Range low -- only meaningful while price is actually IN the range.
    if d.get("in_consolidation"):
        raw.append((_num(d.get("range_low")), "range", "Range low", config.ZONE_WEIGHTS["range"]))

    # Moving averages. EMA8 is deliberately a lighter family of its own: it is too
    # quick to be structure, and it would otherwise lend a pivot-grade 2.0 to what
    # is really just "price is near price".
    raw.append((_num(emas.get("ema21")), "ema", "EMA21", config.ZONE_WEIGHTS["ema"]))
    raw.append((_num(emas.get("ema50")), "ema", "EMA50", config.ZONE_WEIGHTS["ema"]))
    raw.append((_num(weekly_ema21), "ema", "Weekly EMA21", config.ZONE_WEIGHTS["ema"]))
    raw.append((_num(emas.get("ema8")), "ema_fast", "EMA8", config.ZONE_WEIGHTS["ema_fast"]))

    # Volume-weighted levels -- the only ones that know where shares changed hands.
    raw.append((_num(avwap_10d), "vwap", "10d AVWAP", config.ZONE_WEIGHTS["vwap"]))
    raw.append(
        (_num(session_vwap), "session_vwap", "Session VWAP", config.ZONE_WEIGHTS["session_vwap"])
    )

    # Prior session low.
    if daily is not None and len(daily) >= 2:
        raw.append(
            (
                _num(daily["low"].iloc[-2]),
                "prior_day",
                "Prior day low",
                config.ZONE_WEIGHTS["prior_day"],
            )
        )

    levels = [
        {"price": p, "family": fam, "label": label, "weight": weight}
        for (p, fam, label, weight) in raw
        if p is not None and lo <= p <= hi
    ]
    levels.sort(key=lambda x: x["price"])
    return levels


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #
def _cluster(levels: list[dict], tol: float) -> list[dict]:
    """
    Greedily join levels that sit within `tol` of the running cluster.

    Chains from the cluster's CURRENT top rather than from its first member, so a
    tight ladder of levels can grow one band instead of fragmenting into pairs.
    """
    clusters: list[dict] = []
    for lv in levels:
        if clusters and lv["price"] - clusters[-1]["hi"] <= tol:
            c = clusters[-1]
            c["hi"] = lv["price"]
            c["members"].append(lv)
        else:
            clusters.append({"lo": lv["price"], "hi": lv["price"], "members": [lv]})
    return clusters


def _score(members: list[dict]) -> tuple[float, int]:
    """
    (score, anchor_family_count), deduped by family -- the strongest member of each
    family counts, the rest are free.

    Two separate corrections, both about the same disease (agreement that is really
    just restatement):

    1. FAMILY DEDUPE. A cluster holding EMA8+EMA21+EMA50 would otherwise score 5.0
       and outrank a real pivot low. All three are transforms of one close series:
       they agree by construction, which is not confluence.

    2. CONFIRMING CAP. Session VWAP, the prior-day low and the EMA8 sit near price
       every day no matter what the chart is doing, so they would hand a free 4.0 to
       whatever band happens to surround the current print -- producing a "strong"
       zone at price for essentially every ticker, and a zone pinned to price cannot
       distinguish "extended" from "at support". They confirm; they do not create.

    Only ANCHOR families are counted toward the returned family count, so the
    "strong" tier means structural agreement rather than same-day coincidence.
    """
    best: dict[str, float] = {}
    for m in members:
        fam = m["family"]
        if m["weight"] > best.get(fam, 0.0):
            best[fam] = m["weight"]

    anchors = {f: w for f, w in best.items() if f in config.ZONE_ANCHOR_FAMILIES}
    confirming = sum(w for f, w in best.items() if f not in config.ZONE_ANCHOR_FAMILIES)
    score = sum(anchors.values()) + min(confirming, config.ZONE_CONFIRMING_MAX)
    return float(score), len(anchors)


def _touch_count(daily: pd.DataFrame, zone_low: float, zone_high: float) -> int:
    """
    How many DISTINCT times price has come down into this band and been pushed back
    out the top of it.

    This is the difference between "levels agree here" and "this level holds". A band
    no one has ever defended is a coincidence of arithmetic.

    A test requires price to have LEFT the band (a bar trading entirely above it) and
    then come back DOWN into it. That "armed" requirement is the whole point: without
    it, a band sitting at the current price of a choppy name catches every oscillation
    and reports "held 17x", when the truth is that price simply lives there. Fifteen
    consecutive bars drifting in and out of the band are not fifteen tests of support
    -- they are one unresolved argument.

    A test resolves when a bar closes above the band (scores 1) or below it (scores
    nothing -- that is the level failing, the opposite of the evidence sought). A
    close INSIDE the band resolves nothing and the test stays open. Either way price
    must clear the band again before another test can count.
    """
    if daily is None or len(daily) == 0:
        return 0
    try:
        window = daily.tail(config.STRUCTURE_LOOKBACK_DAILY)
        lows = pd.to_numeric(window["low"], errors="coerce").to_numpy(dtype=float)
        closes = pd.to_numeric(window["close"], errors="coerce").to_numpy(dtype=float)
    except (KeyError, TypeError, ValueError):
        return 0

    # A wick slightly THROUGH the band still tests it; a collapse well below does not.
    floor = zone_low * 0.97
    holds = 0
    armed = False     # price has been clear above the band since the last test
    testing = False

    def _resolve(close: float) -> tuple[bool, bool, int]:
        """(still_testing, still_armed, credit) for a bar inside an open test."""
        if close > zone_high:
            return False, False, 1     # pushed back out the top -- it held
        if close < zone_low:
            return False, False, 0     # closed through it -- it broke
        return True, False, 0          # closed inside -- unresolved

    for low, close in zip(lows, closes):
        if not np.isfinite(low) or not np.isfinite(close):
            continue
        if testing:
            testing, armed, credit = _resolve(close)
            holds += credit
        elif low > zone_high:
            armed = True               # cleared the band -- a fresh approach can count
        elif armed and low >= floor:
            testing, armed, credit = _resolve(close)
            holds += credit
        elif low < floor:
            armed = False              # gapped well below; not an approach from above
    return holds


def _classify_status(price: float, zone_low: float, zone_high: float, atr14: float) -> str:
    buffer = config.ZONE_IN_ZONE_BUFFER_ATR * atr14
    if price < zone_low:
        # Price has lost the confluence it was built on. Not a veto (the downtrend
        # gate already covers distribution) but it must not read as neutral.
        return "below_zone"
    if price <= zone_high + buffer:
        return "in_zone"
    return "extended"


def _quality(score: float, anchors: int) -> str:
    """
    `anchors` counts only structural families (config.ZONE_ANCHOR_FAMILIES), so
    "strong" cannot be reached by levels that hug the current price by construction.
    """
    if score >= config.ZONE_STRONG_SCORE and anchors >= config.ZONE_STRONG_MIN_ANCHORS:
        return "strong"
    if score >= config.ZONE_MODERATE_SCORE:
        return "moderate"
    return "weak"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build(
    daily: pd.DataFrame,
    structure: dict,
    *,
    price: float,
    atr14: Optional[float],
    emas: dict,
    weekly_ema21: Optional[float] = None,
    avwap_10d: Optional[float] = None,
    session_vwap: Optional[float] = None,
) -> dict:
    """
    The entry zone for one symbol.

    Returns {"available": False, "status": "none"} when no cluster survives -- which
    is a real answer, not a failure: "no defined pullback support within 3 ATR" is
    exactly what a name at new highs looks like, and saying so is more useful than
    inventing a band.
    """
    try:
        p = _num(price)
        atr = _num(atr14)
        if p is None or atr is None:
            # Without ATR there is no scale: no pullback window, no cluster
            # tolerance, no band width. Every threshold here is ATR-relative.
            return {"available": False, "status": "none"}

        levels = _collect_levels(
            price=p,
            atr14=atr,
            daily=daily,
            structure=structure or {},
            emas=emas or {},
            weekly_ema21=weekly_ema21,
            avwap_10d=avwap_10d,
            session_vwap=session_vwap,
        )
        if not levels:
            return {"available": False, "status": "none"}

        tol = max(config.ZONE_TOL_PCT / 100.0 * p, config.ZONE_TOL_ATR * atr)
        clusters = _cluster(levels, tol)
        if not clusters:
            return {"available": False, "status": "none"}

        scored = []
        for c in clusters:
            score, anchors = _score(c["members"])
            lo, hi = c["lo"], c["hi"]
            # A single level is a line, not an area -- and an area is what was
            # asked for. Widen it so the band can actually be traded to.
            if hi - lo < 2 * config.ZONE_MIN_HALF_WIDTH_ATR * atr:
                mid = (lo + hi) / 2.0
                half = config.ZONE_MIN_HALF_WIDTH_ATR * atr
                lo, hi = mid - half, mid + half
            touches = _touch_count(daily, lo, hi)
            score += config.ZONE_TOUCH_WEIGHT * min(touches, config.ZONE_MAX_TOUCHES_SCORED)
            scored.append(
                {
                    "lo": lo,
                    "hi": hi,
                    "score": score,
                    "anchors": anchors,
                    "touches": touches,
                    "members": c["members"],
                }
            )

        # Best score wins; ties break toward the NEARER band -- a zone you might
        # actually get filled at beats an equally good one 3 ATR away.
        best = max(scored, key=lambda c: (c["score"], -abs(p - (c["lo"] + c["hi"]) / 2.0)))

        zone_low = round(best["lo"], 2)
        zone_high = round(best["hi"], 2)
        zone_mid = round((zone_low + zone_high) / 2.0, 2)
        status = _classify_status(p, zone_low, zone_high, atr)

        # Distance to the band's NEAR edge (its top), not its middle: the top is
        # where a limit order would fill. Negative = price is above the zone.
        dist = (zone_high - p) / p * 100.0

        return {
            "available": True,
            "zone_low": zone_low,
            "zone_high": zone_high,
            "zone_mid": zone_mid,
            "status": status,
            "quality": _quality(best["score"], best["anchors"]),
            "score": _round(best["score"], 2),
            "anchors": int(best["anchors"]),
            "touches": int(best["touches"]),
            "levels": [
                f"{m['label']} {m['price']:.2f}"
                for m in sorted(best["members"], key=lambda m: -m["weight"])
            ],
            "dist_to_zone_pct": _round(dist),
            "dist_to_zone_atr": _round((p - zone_high) / atr),
            # A stop for an entry AT the zone must sit below the zone, not below the
            # current price. Offered here; claude_engine decides what to do with it.
            "stop_below_zone": _round(zone_low - config.ZONE_STOP_ATR_BUFFER * atr),
        }
    except Exception as exc:  # defensive: the zone must never kill a ticker
        logger.warning("Entry-zone build failed: %s", exc)
        return {"available": False, "status": "none"}

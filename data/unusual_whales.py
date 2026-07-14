"""
Unusual Whales (UW) data fetcher.

Pulls, per ticker:
  * Current implied volatility and 52-week IV rank.
  * Put/call ratio.
  * Unusual *bullish* flow alerts from the past N days with premium > threshold.
  * Top open-interest strikes with expirations.

UW response shapes vary by plan and evolve over time, so every parse here is
defensive: it probes several likely field names and returns None / empty rather
than raising. Endpoint paths are configurable in config.py so they can be
adjusted to match your subscription without touching this module.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any, Optional

import aiohttp

import config
from data import pricing

logger = logging.getLogger("flowscanner.uw")


# --------------------------------------------------------------------------- #
# OCC option symbols
# --------------------------------------------------------------------------- #
# UW's option-contracts rows carry NO `strike` field -- strike, expiry and
# call/put are all encoded in the OCC symbol:
#
#     AAPL260715C00315000
#     ^root ^YYMMDD    ^strike x 1000, zero-padded to 8
#              ^C|P
#
# Parsing this is what makes the whole chain usable. Looking for a "strike" key
# (as this module originally did) silently dropped every contract, leaving the
# open-interest list empty on every ticker.
_OCC_RE = re.compile(r"^(?P<root>[A-Z.]+)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def _decode_occ(symbol: Any) -> Optional[dict]:
    """Decode an OCC option symbol into {expiry, type, strike}, or None."""
    m = _OCC_RE.match(str(symbol or "").strip().upper())
    if not m:
        return None
    try:
        expiry = dt.datetime.strptime(m["ymd"], "%y%m%d").date()
    except ValueError:
        return None
    return {
        "expiry": expiry,
        "type": "call" if m["cp"] == "C" else "put",
        "strike": int(m["strike"]) / 1000.0,
    }


# Greeks/pricing live in data/pricing.py so the chain builder and the trade-card
# validator price the same contract the same way.
_call_delta = pricing.call_delta


def _first(d: dict, *keys, default=None):
    """Return the first present, non-null value among keys."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _unwrap(payload: Any) -> Any:
    """
    Normalize a UW payload to its rows. Most endpoints wrap a list under 'data'
    (sometimes 'chains'/'results'); a few single-object endpoints (e.g.
    volatility/stats) wrap a *dict* under 'data' instead. Return a list unchanged
    (callers iterate / index it), or return the dict directly so single-object
    endpoints can be read without indexing.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "chains", "results", "flow_alerts"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                return val
        # single object -> wrap
        return [payload]
    return []


class UnusualWhalesClient:
    """Async UW client with rate limiting, retries, and defensive parsing."""

    def __init__(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self._session = session
        self._sem = semaphore
        self._limiter = config.AsyncRateLimiter(config.UW_RATE_PER_MIN)
        self._headers = {
            "Authorization": f"Bearer {config.UW_API_KEY}",
            "Accept": "application/json",
        }

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        url = f"{config.UW_BASE}{path}"
        for attempt in range(1, config.HTTP_RETRIES + 2):
            try:
                await self._limiter.acquire()
                async with self._sem:
                    async with self._session.get(
                        url, params=params or {}, headers=self._headers
                    ) as resp:
                        if resp.status == 429:
                            wait = min(30, 2 ** attempt)
                            logger.warning("UW 429 on %s, backing off %ss", path, wait)
                            await asyncio.sleep(wait)
                            continue
                        if resp.status in (401, 403):
                            logger.error("UW auth error (%s) on %s -- check UW_API_KEY", resp.status, path)
                            return None
                        if resp.status != 200:
                            body = (await resp.text())[:200]
                            logger.warning("UW %s -> HTTP %s: %s", path, resp.status, body)
                            return None
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("UW request error on %s (attempt %s): %s", path, attempt, exc)
                await asyncio.sleep(min(10, 2 ** attempt))
        logger.error("UW request permanently failed: %s", path)
        return None

    # ---------------------------------------------------------------- #
    # Individual field fetchers
    # ---------------------------------------------------------------- #
    async def _volatility_stats(self, ticker: str) -> dict:
        """
        Raw IV / IV-rank snapshot from the volatility/stats endpoint. This is the
        source of truth for IV -- the options-volume payload carries no IV fields.
        volatility/stats wraps a single object under 'data', which _unwrap returns
        directly. Returns the data dict, or {} on failure.
        """
        path = config.UW_VOLATILITY_STATS.format(ticker=ticker)
        payload = await self._get(path)
        data = _unwrap(payload)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}

    async def _options_volume(self, ticker: str) -> dict:
        """
        IV, IV rank and put/call ratio. IV and IV rank come from the dedicated
        volatility/stats endpoint; the put/call ratio is derived from
        options-volume (which has no IV fields).
        """
        path = config.UW_OPTIONS_VOLUME.format(ticker=ticker)
        payload = await self._get(path)
        rows = _unwrap(payload)
        rows = rows if isinstance(rows, list) else [rows]

        # Diagnostic: dump the exact options-volume response for AAPL.
        if ticker.upper() == "AAPL":
            logger.debug("UW %s raw payload for AAPL: %r", path, payload)
            if rows:
                logger.debug(
                    "UW AAPL first-row keys: %s", sorted(rows[0].keys())
                    if isinstance(rows[0], dict) else type(rows[0]).__name__
                )

        row = rows[0] if rows else {}  # most recent options-volume snapshot

        # IV / IV rank from volatility/stats (fields 'iv', 'iv_rank' already match).
        stats = await self._volatility_stats(ticker)

        # NO SCALE GUESSING. UW returns IV as a fraction (0.28 = 28%; values above 1.0
        # are real -- 1.6 = 160% IV is common on a squeeze name). The old
        # `if iv > 5: iv /= 100` would have turned a genuine 600% IV into 0.06, i.e.
        # the CHEAPEST premium in the universe, and the rubric rewards cheap IV --
        # a fully inverted signal on exactly the most dangerous names.
        iv = _to_float(_first(stats, "implied_volatility", "iv", "avg_iv", "atm_iv"))
        if iv is not None and iv > config.IV_SANITY_MAX:
            logger.warning("%s: implausible IV %.2f from UW; dropping the field", ticker, iv)
            iv = None

        # UW's iv_rank is natively 0-100 (observed range 15.7 .. 100.0). The old
        # `if iv_rank <= 1.0: iv_rank *= 100` rescaled by magnitude, so a genuine rank
        # of 0.8 -- IV pinned at its 52-week LOW, the cheapest-premium name in the
        # universe and exactly what the strategy hunts -- became 80.0 = "expensive".
        # The bug fired only on the best entries. Take the value as given.
        iv_rank = _to_float(_first(stats, "iv_rank", "iv_rank_52w", "iv_rank_1y"))
        if iv_rank is not None and not (0.0 <= iv_rank <= 100.0):
            logger.warning("%s: iv_rank %.2f outside 0-100; dropping", ticker, iv_rank)
            iv_rank = None

        pc_ratio = _to_float(
            _first(row, "put_call_ratio", "pc_ratio", "put_call_ratio_volume")
        )
        if pc_ratio is None:
            put_v = _to_float(_first(row, "put_volume", "puts_volume"))
            call_v = _to_float(_first(row, "call_volume", "calls_volume"))
            if put_v is not None and call_v not in (None, 0):
                pc_ratio = round(put_v / call_v, 3)

        # UW computes its own directional premium aggregates. These are the honest
        # answer to the prompt's "directionality of the flow" question.
        bull_prem = _to_float(row.get("bullish_premium"))
        bear_prem = _to_float(row.get("bearish_premium"))
        net_call = _to_float(row.get("net_call_premium"))
        net_put = _to_float(row.get("net_put_premium"))

        return {
            "iv": round(iv, 4) if iv is not None else None,
            "iv_rank": round(iv_rank, 1) if iv_rank is not None else None,
            "put_call_ratio": round(pc_ratio, 3) if pc_ratio is not None else None,
            "bullish_premium": round(bull_prem, 0) if bull_prem is not None else None,
            "bearish_premium": round(bear_prem, 0) if bear_prem is not None else None,
            "net_call_premium_day": round(net_call, 0) if net_call is not None else None,
            "net_put_premium_day": round(net_put, 0) if net_put is not None else None,
        }

    async def _option_chain(self, ticker: str, spot: Optional[float]) -> dict:
        """
        Fetch and decode the option chain. Returns:

          top_oi         -- highest open-interest strikes (the OI clusters the
                            analysis prompt reasons about)
          call_candidates-- the actual, tradable calls inside the DTE window and
                            near the money, each with real bid/ask, per-contract
                            IV, open interest, and a computed Black-Scholes delta.

        `call_candidates` is what makes contract selection real: the model picks
        one of these rather than inventing a strike, an expiration and a delta
        that may not correspond to any listed contract.
        """
        path = config.UW_OPTION_CONTRACTS.format(ticker=ticker)
        payload = await self._get(path, {"limit": config.CHAIN_FETCH_LIMIT})
        rows = _unwrap(payload)
        if not isinstance(rows, list):
            return {"top_oi": [], "call_candidates": []}

        today = dt.date.today()
        decoded: list[dict] = []
        undecodable = 0
        for r in rows:
            occ = _decode_occ(_first(r, "option_symbol", "symbol", "ticker"))
            if occ is None:
                undecodable += 1
                continue
            oi = _to_float(_first(r, "open_interest", "oi"))
            if oi is None:
                continue
            bid = _to_float(_first(r, "nbbo_bid", "bid"))
            ask = _to_float(_first(r, "nbbo_ask", "ask"))
            iv = _to_float(_first(r, "implied_volatility", "iv"))
            decoded.append(
                {
                    "strike": occ["strike"],
                    "expiry": occ["expiry"].isoformat(),
                    "dte": (occ["expiry"] - today).days,
                    "type": occ["type"],
                    "open_interest": int(oi),
                    "bid": bid,
                    "ask": ask,
                    "iv": round(iv, 4) if iv is not None else None,
                }
            )

        if undecodable and not decoded:
            logger.warning(
                "%s: no option symbols decoded from %s rows -- OCC format may have "
                "changed; contract selection will fall back to price only",
                ticker,
                len(rows),
            )

        top_oi = sorted(decoded, key=lambda c: c["open_interest"], reverse=True)
        top_oi = [
            {k: c[k] for k in ("strike", "expiry", "open_interest", "type")}
            for c in top_oi[: config.TOP_OI_STRIKES]
        ]

        candidates: list[dict] = []
        if spot and spot > 0:
            for c in decoded:
                if c["type"] != "call":
                    continue
                if not (config.MIN_DTE <= c["dte"] <= config.MAX_DTE):
                    continue
                moneyness = c["strike"] / spot - 1.0
                if not (config.CHAIN_MIN_MONEYNESS <= moneyness <= config.CHAIN_MAX_MONEYNESS):
                    continue
                if c["open_interest"] < config.CHAIN_MIN_OPEN_INTEREST:
                    continue
                # An ask of zero/None means no live offer -- not buyable.
                if not c["ask"]:
                    continue
                mid = round((c["bid"] + c["ask"]) / 2.0, 2) if c["bid"] else c["ask"]
                candidates.append(
                    {
                        **c,
                        "mid": mid,
                        "moneyness_pct": round(moneyness * 100.0, 1),
                        "delta": _call_delta(spot, c["strike"], c["dte"], c["iv"] or 0.0),
                        # What the underlying must reach by expiry to break even.
                        "breakeven": round(c["strike"] + c["ask"], 2),
                        "spread_pct": (
                            round((c["ask"] - c["bid"]) / c["ask"] * 100.0, 1)
                            if c["bid"] and c["ask"]
                            else None
                        ),
                    }
                )
            # Liquidity first: the model should be choosing among contracts that
            # can actually be filled at something like the quoted price.
            candidates.sort(key=lambda c: c["open_interest"], reverse=True)
            candidates = candidates[: config.CHAIN_MAX_CANDIDATES]

        return {"top_oi": top_oi, "call_candidates": candidates}

    async def _flow_alerts(self, ticker: str) -> dict:
        """
        Unusual flow alerts, split by ACTUAL DIRECTION.

        The previous test was
            is_bullish = ("call" in opt_type) or ("bull" in sentiment) or ("ask" in sentiment)
        UW's flow-alert rows carry no `sentiment` field at all, so `_first` fell
        through to `alert_rule` -- whose values are RepeatedHits /
        RepeatedHitsAscendingFill / RepeatedHitsDescendingFill, none of which contain
        "bull" or "ask". Both of those clauses were dead, and the test collapsed to
        "is it a call?". A fund SELLING $40M of calls (bearish, caps upside) was added
        to `total_bull_premium` and stamped `"sentiment": "bullish"`. The category
        fired on 100% of the universe and discriminated nothing.

        Direction is available per row: `total_ask_side_prem` (lifted the offer =
        BOUGHT) vs `total_bid_side_prem` (hit the bid = SOLD). Bullish call flow is
        calls BOUGHT. We now count only that, and surface bought puts as a bearish
        counterweight so the model can see both sides.
        """
        path = config.UW_FLOW_ALERTS.format(ticker=ticker)
        payload = await self._get(path, {"limit": config.FLOW_FETCH_LIMIT})
        if payload is None:
            # Distinguish "fetch failed" from "no unusual flow" -- reporting a hard
            # zero for a 503 tells the model there is no institutional interest.
            return {"flow_available": False}
        rows = _unwrap(payload)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.FLOW_LOOKBACK_DAYS)

        alerts: list[dict] = []
        bought_call_premium = 0.0
        sold_call_premium = 0.0
        bought_put_premium = 0.0
        for r in rows:
            opt_type = str(_first(r, "option_type", "type", default="")).lower()
            ask_prem = _to_float(r.get("total_ask_side_prem")) or 0.0
            bid_prem = _to_float(r.get("total_bid_side_prem")) or 0.0
            total_prem = _to_float(_first(r, "total_premium", "premium")) or 0.0

            ts_raw = _first(r, "created_at", "timestamp", "start_time", "executed_at")
            parsed = _parse_ts(ts_raw) if ts_raw else None
            if parsed is None or parsed < cutoff:
                # Fail CLOSED on an unparseable timestamp: an alert we can't date
                # could be arbitrarily stale, and stale flow is not a signal.
                continue
            if total_prem < config.MIN_FLOW_PREMIUM:
                continue

            # Which side of the spread did the size trade on?
            if ask_prem > bid_prem:
                side, directional_prem = "bought", ask_prem
            elif bid_prem > ask_prem:
                side, directional_prem = "sold", bid_prem
            else:
                side, directional_prem = "mixed", 0.0

            is_call = "call" in opt_type
            is_put = "put" in opt_type
            if is_call and side == "bought":
                bought_call_premium += directional_prem
                sentiment = "bullish"          # calls bought = bullish
            elif is_call and side == "sold":
                sold_call_premium += directional_prem
                sentiment = "bearish"          # calls sold = capping upside
            elif is_put and side == "bought":
                bought_put_premium += directional_prem
                sentiment = "bearish"          # puts bought = bearish
            elif is_put and side == "sold":
                sentiment = "bullish"          # puts sold = bullish
            else:
                sentiment = "neutral"

            alerts.append(
                {
                    "type": "call" if is_call else ("put" if is_put else opt_type or "unknown"),
                    "side": side,
                    "sentiment": sentiment,
                    "premium": round(total_prem, 0),
                    "ask_side_premium": round(ask_prem, 0),
                    "bid_side_premium": round(bid_prem, 0),
                    "strike": _to_float(_first(r, "strike", "strike_price")),
                    "expiry": str(_first(r, "expiry", "expiration", default=""))[:10] or None,
                }
            )

        bullish = [a for a in alerts if a["sentiment"] == "bullish"]
        bearish = [a for a in alerts if a["sentiment"] == "bearish"]
        # Show the model the biggest prints from BOTH sides, not just the bullish ones.
        alerts.sort(key=lambda a: a["premium"], reverse=True)
        return {
            "flow_available": True,
            "flow_alerts": alerts[: config.FLOW_ALERTS_SHOWN],
            "flow_count": len(alerts),
            "bullish_alert_count": len(bullish),
            "bearish_alert_count": len(bearish),
            "bought_call_premium": round(bought_call_premium, 0),
            "sold_call_premium": round(sold_call_premium, 0),
            "bought_put_premium": round(bought_put_premium, 0),
            # Net directional call premium: bought minus sold. NEGATIVE means the
            # institutional call flow was net SELLING -- a headwind, not a tailwind.
            "net_call_premium": round(bought_call_premium - sold_call_premium, 0),
        }

    # ---------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------- #
    async def get_options_data(self, ticker: str, spot: Optional[float] = None) -> dict:
        """
        Fetch the full UW feature set for one ticker. `spot` is the current
        underlying price (from FMP) -- it is what lets the chain be filtered to
        near-the-money calls and their deltas computed. Without it the chain is
        still returned as OI clusters, but no tradable candidates are offered.

        Never raises: on failure a field is simply absent/None so the ticker can
        still be analyzed.
        """
        try:
            vol, chain, flow = await asyncio.gather(
                self._options_volume(ticker),
                self._option_chain(ticker, spot),
                self._flow_alerts(ticker),
            )
        except Exception as exc:  # defensive
            logger.exception("Unexpected UW error for %s: %s", ticker, exc)
            vol, chain, flow = {}, {}, {}

        data: dict[str, Any] = {
            "iv": vol.get("iv"),
            "iv_rank": vol.get("iv_rank"),
            "put_call_ratio": vol.get("put_call_ratio"),
            # UW's own day-level directional aggregates.
            "bullish_premium": vol.get("bullish_premium"),
            "bearish_premium": vol.get("bearish_premium"),
            "net_call_premium_day": vol.get("net_call_premium_day"),
            "net_put_premium_day": vol.get("net_put_premium_day"),
            "top_oi": chain.get("top_oi", []),
            "call_candidates": chain.get("call_candidates", []),
            # False when the flow fetch FAILED. A hard zero would tell the model there
            # is no institutional interest, which is a different (and wrong) claim.
            "flow_available": flow.get("flow_available", False),
            "flow_alerts": flow.get("flow_alerts", []),
            "flow_count": flow.get("flow_count", 0),
            "bullish_alert_count": flow.get("bullish_alert_count", 0),
            "bearish_alert_count": flow.get("bearish_alert_count", 0),
            "bought_call_premium": flow.get("bought_call_premium", 0.0),
            "sold_call_premium": flow.get("sold_call_premium", 0.0),
            "bought_put_premium": flow.get("bought_put_premium", 0.0),
            "net_call_premium": flow.get("net_call_premium", 0.0),
        }
        return data


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    """Parse ISO-8601 or epoch timestamps into an aware UTC datetime."""
    try:
        if isinstance(value, (int, float)):
            # epoch seconds or millis
            if value > 1e12:
                value = value / 1000.0
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        s = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (ValueError, OverflowError, OSError):
        return None

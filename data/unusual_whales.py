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
from typing import Any, Optional

import aiohttp

import config

logger = logging.getLogger("flowscanner.uw")


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
        iv = _to_float(_first(stats, "implied_volatility", "iv", "avg_iv", "atm_iv"))
        if iv is not None and iv > 5:  # normalize percentage form to fraction if needed
            iv = iv / 100.0
        iv_rank = _to_float(_first(stats, "iv_rank", "iv_rank_52w", "iv_percentile", "iv_rank_1y"))
        if iv_rank is not None and iv_rank <= 1.0:
            iv_rank = iv_rank * 100.0  # normalize 0-1 -> 0-100

        if ticker.upper() == "AAPL":
            logger.info("UW AAPL resolved IV=%s IV_rank=%s", iv, iv_rank)

        pc_ratio = _to_float(
            _first(row, "put_call_ratio", "pc_ratio", "put_call_ratio_volume")
        )
        if pc_ratio is None:
            put_v = _to_float(_first(row, "put_volume", "puts_volume"))
            call_v = _to_float(_first(row, "call_volume", "calls_volume"))
            if put_v is not None and call_v not in (None, 0):
                pc_ratio = round(put_v / call_v, 3)
        return {
            "iv": round(iv, 4) if iv is not None else None,
            "iv_rank": round(iv_rank, 1) if iv_rank is not None else None,
            "put_call_ratio": round(pc_ratio, 3) if pc_ratio is not None else None,
        }

    async def _top_open_interest(self, ticker: str) -> list[dict]:
        """Top open-interest strikes with expirations."""
        path = config.UW_OPTION_CONTRACTS.format(ticker=ticker)
        payload = await self._get(path, {"limit": 500})
        rows = _unwrap(payload)
        contracts = []
        for r in rows:
            oi = _to_float(_first(r, "open_interest", "oi"))
            strike = _to_float(_first(r, "strike", "strike_price"))
            expiry = _first(r, "expiry", "expiration", "expires")
            opt_type = _first(r, "option_type", "type", "side", default="")
            if oi is None or strike is None:
                continue
            contracts.append(
                {
                    "strike": strike,
                    "expiry": str(expiry)[:10] if expiry else None,
                    "open_interest": int(oi),
                    "type": str(opt_type).lower(),
                }
            )
        contracts.sort(key=lambda c: c["open_interest"], reverse=True)
        return contracts[: config.TOP_OI_STRIKES]

    async def _flow_alerts(self, ticker: str) -> dict:
        """Bullish unusual flow alerts within the lookback window over the premium floor."""
        path = config.UW_FLOW_ALERTS.format(ticker=ticker)
        payload = await self._get(path, {"limit": 100})
        rows = _unwrap(payload)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.FLOW_LOOKBACK_DAYS)

        alerts = []
        total_bull_premium = 0.0
        for r in rows:
            premium = _to_float(_first(r, "total_premium", "premium", "total_ask_side_prem"))
            opt_type = str(_first(r, "option_type", "type", "side", default="")).lower()
            sentiment = str(_first(r, "sentiment", "alert_rule", "tags", default="")).lower()
            ts_raw = _first(r, "created_at", "timestamp", "start_time", "executed_at")

            # timestamp filter (best effort)
            in_window = True
            if ts_raw:
                parsed = _parse_ts(ts_raw)
                if parsed is not None:
                    in_window = parsed >= cutoff
            if not in_window:
                continue

            is_bullish = ("call" in opt_type) or ("bull" in sentiment) or ("ask" in sentiment)
            if not is_bullish:
                continue
            if premium is None or premium < config.MIN_FLOW_PREMIUM:
                continue

            total_bull_premium += premium
            alerts.append(
                {
                    "type": "call" if "call" in opt_type else (opt_type or "call"),
                    "premium": round(premium, 0),
                    "strike": _to_float(_first(r, "strike", "strike_price")),
                    "expiry": str(_first(r, "expiry", "expiration", default=""))[:10] or None,
                    "sentiment": "bullish",
                }
            )

        alerts.sort(key=lambda a: a["premium"], reverse=True)
        return {
            "flow_alerts": alerts[:10],
            "flow_count": len(alerts),
            "total_bull_premium": round(total_bull_premium, 0),
        }

    # ---------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------- #
    async def get_options_data(self, ticker: str) -> dict:
        """
        Fetch the full UW feature set for one ticker. Never raises: on failure a
        field is simply absent/None so the ticker can still be analyzed.
        """
        try:
            vol, oi, flow = await asyncio.gather(
                self._options_volume(ticker),
                self._top_open_interest(ticker),
                self._flow_alerts(ticker),
            )
        except Exception as exc:  # defensive
            logger.exception("Unexpected UW error for %s: %s", ticker, exc)
            vol, oi, flow = {}, [], {}

        data: dict[str, Any] = {
            "iv": vol.get("iv"),
            "iv_rank": vol.get("iv_rank"),
            "put_call_ratio": vol.get("put_call_ratio"),
            "top_oi": oi or [],
            "flow_alerts": flow.get("flow_alerts", []),
            "flow_count": flow.get("flow_count", 0),
            "total_bull_premium": flow.get("total_bull_premium", 0.0),
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

"""
VIX fetcher (Yahoo Finance).

Pulls the latest ^VIX close via Yahoo's public chart API and classifies the
volatility regime for macro context. Fully async and fault-tolerant.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger("flowscanner.vix")

# A browser-like UA avoids occasional Yahoo bot rejections.
_HEADERS = {"User-Agent": "Mozilla/5.0 (FlowScanner)"}


def classify_regime(level: Optional[float]) -> str:
    if level is None:
        return "unknown"
    if level < 15:
        return "low (risk-on)"
    if level < 20:
        return "normal"
    if level < 28:
        return "elevated"
    return "high (risk-off)"


async def get_vix(session: aiohttp.ClientSession) -> dict:
    """Return {'level': float|None, 'regime': str, 'change_pct': float|None}."""
    params = {"interval": "1d", "range": "5d"}
    for attempt in range(1, config.HTTP_RETRIES + 2):
        try:
            async with session.get(
                config.YAHOO_CHART, params=params, headers=_HEADERS
            ) as resp:
                if resp.status != 200:
                    logger.warning("VIX fetch HTTP %s", resp.status)
                    await asyncio.sleep(2 ** attempt)
                    continue
                payload = await resp.json()
                result = payload.get("chart", {}).get("result")
                if not result:
                    logger.warning("VIX payload missing 'result'")
                    return {"level": None, "regime": "unknown", "change_pct": None}
                quote = result[0]
                closes = (
                    quote.get("indicators", {})
                    .get("quote", [{}])[0]
                    .get("close", [])
                )
                closes = [c for c in closes if c is not None]
                if not closes:
                    meta = quote.get("meta", {})
                    level = meta.get("regularMarketPrice")
                    return {
                        "level": round(level, 2) if level else None,
                        "regime": classify_regime(level),
                        "change_pct": None,
                    }
                level = float(closes[-1])
                prev = float(closes[-2]) if len(closes) >= 2 else level
                change_pct = round((level / prev - 1.0) * 100.0, 2) if prev else None
                return {
                    "level": round(level, 2),
                    "regime": classify_regime(level),
                    "change_pct": change_pct,
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("VIX fetch error (attempt %s): %s", attempt, exc)
            await asyncio.sleep(2 ** attempt)

    logger.error("VIX fetch permanently failed")
    return {"level": None, "regime": "unknown", "change_pct": None}

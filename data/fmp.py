"""
Financial Modeling Prep (FMP) data fetcher.

Responsibilities:
  * Screen the tradable universe (market cap / volume / price / exchange).
  * Pull daily OHLCV (and resample to weekly) plus intraday 1-hour candles.
  * Compute technical indicators locally from OHLCV: RSI(14) daily & weekly,
    MACD(12/26/9) daily, ATR(14) daily, EMA 8/21/50/200 daily.
  * Derive daily & weekly swing structure (pivots, trend sequence, consolidation,
    support/resistance) via data.structure.
  * Pull the earnings calendar and build a symbol -> nearest-upcoming-date map.

Indicators are computed locally from FMP OHLCV rather than relying on FMP's
technical-indicator endpoints, which keeps MACD/ATR available on every plan and
guarantees consistent math across the whole universe.

All network access is async, rate-limited, and fault-tolerant: a failure for a
single symbol is logged and returns None rather than crashing the scan.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Optional

import aiohttp
import numpy as np
import pandas as pd

import config
from data import entry_zone, structure

logger = logging.getLogger("flowscanner.fmp")


# --------------------------------------------------------------------------- #
# Indicator helpers (pure pandas / numpy)
# --------------------------------------------------------------------------- #
def _ema(series: pd.Series, span: int) -> pd.Series:
    """
    EMA that refuses to invent a value it cannot support.

    `ewm(adjust=False)` seeds the recursion from the FIRST observation, so without
    min_periods it emits a plausible-looking number from bar 1 -- a 40-bar IPO would
    report an "EMA200". That number is almost entirely the seed bar, and it fed the
    scanner's load-bearing trend gate (price_above_ema200 / ema_stack_bullish).
    min_periods=span makes an unsupported EMA NaN, which _round() turns into None.
    """
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI. Zero average loss means there were NO down bars -- RSI is 100
    (maximally overbought), not 50. The previous code divided by NaN to dodge the
    zero-division and then swallowed the NaN with fillna(50.0), silently laundering
    the single most overbought reading in the scanner into "neutral" -- precisely
    the reading a call scanner most needs to see. (The mirror case, zero gain, gives
    rs=0 -> RSI 0, which was already correct.)
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # avg_loss == 0 -> rs = inf -> rsi = 100. Make that explicit rather than relying
    # on inf arithmetic, and only where there is genuinely a gain to speak of.
    zero_loss = (avg_loss == 0) & avg_gain.notna()
    rsi = rsi.mask(zero_loss & (avg_gain > 0), 100.0)
    rsi = rsi.mask(zero_loss & (avg_gain == 0), 50.0)  # flat line: neutral is right
    # Warm-up bars stay NaN -> _round() -> None. Never a fabricated 50.
    return rsi


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = _ema(series, fast) - _ema(series, slow)
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _vwap(df: pd.DataFrame) -> Optional[float]:
    """
    Volume-weighted average price over `df`, on typical price (h+l+c)/3.

    The only volume-weighted level the scanner has. Every other support level it
    knows (pivots, EMAs) is derived from price alone and so is blind to *where the
    shares actually changed hands* -- which is exactly what makes VWAP act as
    support: it is the average participant's cost basis, not a curve fit.

    Returns None rather than a number whenever the weighting would be a lie: no
    volume column, all-zero volume (a halted or synthetic session), or no usable
    bars. A volume-weighted average with no volume is just an unweighted mean
    wearing its name, and it would be scored as confluence at weight 2.0.
    """
    if df is None or len(df) == 0:
        return None
    if not {"high", "low", "close", "volume"}.issubset(df.columns):
        return None
    try:
        typical = (
            pd.to_numeric(df["high"], errors="coerce")
            + pd.to_numeric(df["low"], errors="coerce")
            + pd.to_numeric(df["close"], errors="coerce")
        ) / 3.0
        volume = pd.to_numeric(df["volume"], errors="coerce")
        ok = typical.notna() & volume.notna() & (volume > 0)
        if not ok.any():
            return None
        total_volume = float(volume[ok].sum())
        if total_volume <= 0:
            return None
        return float((typical[ok] * volume[ok]).sum() / total_volume)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _round(value: Any, digits: int = 2) -> Optional[float]:
    try:
        if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _market_today(hourly: Optional[pd.DataFrame]) -> Optional[dt.date]:
    """
    The exchange's current date, read off the data instead of off the wall clock.

    `dt.date.today()` is the runner's LOCAL date, and the scan runs on a UTC CI
    runner while FMP timestamps are exchange time. Those two agree only because the
    sessions fire at 09:00 and 14:00 ET (13:00-19:00 UTC, same calendar day). A run
    after ~20:00 ET -- a manual `--run now`, a retried dispatch -- is already
    tomorrow in UTC, and every "is this bar today's?" test would flip to False on a
    bar that is very much today's.

    The newest hourly timestamp is the exchange's own answer, so prefer it and fall
    back to the local date only when there are no hourly bars at all. Returns None
    if neither is available -- callers must treat that as "unknown", not "no".
    """
    if hourly is not None and len(hourly) and "date" in hourly.columns:
        try:
            return hourly["date"].iloc[-1].date()
        except (AttributeError, IndexError, ValueError):
            pass
    try:
        return dt.date.today()
    except (OSError, ValueError):  # pragma: no cover -- defensive
        return None


class FMPClient:
    """Async FMP client with rate limiting and retries."""

    def __init__(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        self._session = session
        self._sem = semaphore
        self._limiter = config.AsyncRateLimiter(config.FMP_RATE_PER_MIN)

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        params = dict(params or {})
        params["apikey"] = config.FMP_API_KEY
        url = f"{config.FMP_BASE}/{path.lstrip('/')}"

        for attempt in range(1, config.HTTP_RETRIES + 2):
            try:
                await self._limiter.acquire()
                async with self._sem:
                    async with self._session.get(url, params=params) as resp:
                        if resp.status == 429:
                            wait = min(30, 2 ** attempt)
                            logger.warning("FMP 429 on %s, backing off %ss", path, wait)
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            body = (await resp.text())[:200]
                            logger.warning("FMP %s -> HTTP %s: %s", path, resp.status, body)
                            return None
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("FMP request error on %s (attempt %s): %s", path, attempt, exc)
                await asyncio.sleep(min(10, 2 ** attempt))
        logger.error("FMP request permanently failed: %s", path)
        return None

    # ---------------------------------------------------------------- #
    # Universe screening
    # ---------------------------------------------------------------- #
    async def screen_universe(self) -> list[dict]:
        """Return raw screener rows meeting the configured thresholds."""
        params = {
            "marketCapMoreThan": config.MIN_MARKET_CAP,
            "volumeMoreThan": config.MIN_AVG_VOLUME,
            "priceMoreThan": config.MIN_PRICE,
            "exchange": ",".join(config.EXCHANGES),
            "isActivelyTrading": "true",
            "isEtf": "false",
            "isFund": "false",
            "limit": 3000,
        }
        rows = await self._get("company-screener", params)
        if not isinstance(rows, list):
            logger.error("Screener returned unexpected payload; got %s", type(rows))
            return []
        cleaned = []
        for r in rows:
            symbol = r.get("symbol")
            if not symbol or "." in symbol or "-" in symbol:
                continue  # skip preferreds / warrants / non-common share classes
            cleaned.append(r)
        # Stage breakdown so we can see exactly where the universe shrinks.
        # marketCap / volume / price are enforced server-side by the screener
        # params, so every raw row already clears those thresholds; the only
        # local drop here is non-common share classes. The earnings-exclusion
        # and final-selection stages happen downstream in main._select_universe.
        logger.info(
            "Screener stages: %s raw rows "
            "(passed marketCap>=%s, volume>=%s, price>=%s server-side) "
            "-> %s after dropping non-common share classes",
            len(rows),
            config.MIN_MARKET_CAP,
            config.MIN_AVG_VOLUME,
            config.MIN_PRICE,
            len(cleaned),
        )
        return cleaned

    # ---------------------------------------------------------------- #
    # Earnings calendar
    # ---------------------------------------------------------------- #
    async def earnings_map(self) -> dict[str, str]:
        """
        Map symbol -> nearest upcoming earnings date (ISO string) within the
        lookahead window. Used both to exclude near-term reporters and to attach
        the nearest earnings date per ticker.

        FETCHED IN CHUNKS, and that is load-bearing. FMP caps this endpoint at 4000
        rows and truncates from the NEAR end: a single 90-day request came back
        holding only days 29-90, so every company reporting in the next four weeks
        was missing. The earnings-exclusion filter reads this map, so it could never
        exclude anybody -- AAPL, JPM, GS, MSFT and TSLA all sailed through with
        `days_to_earnings: null` days before reporting. Chunking keeps each response
        far below the cap so the near dates actually arrive.
        """
        today = dt.date.today()
        mapping: dict[str, str] = {}
        chunk = max(1, config.EARNINGS_CHUNK_DAYS)
        any_ok = False

        for start_offset in range(0, config.EARNINGS_LOOKAHEAD_DAYS, chunk):
            frm = today + dt.timedelta(days=start_offset)
            to = min(
                frm + dt.timedelta(days=chunk - 1),
                today + dt.timedelta(days=config.EARNINGS_LOOKAHEAD_DAYS),
            )
            rows = await self._get(
                "earnings-calendar", {"from": frm.isoformat(), "to": to.isoformat()}
            )
            if not isinstance(rows, list):
                logger.warning("Earnings calendar chunk %s..%s unavailable", frm, to)
                continue
            any_ok = True
            if len(rows) >= config.EARNINGS_ROW_CAP:
                # Still truncated -> near dates may be missing again. Shout: silently
                # trusting this map is what let earnings risk through in the first place.
                logger.error(
                    "Earnings chunk %s..%s hit the %s-row cap (%s rows) -- dates are "
                    "being dropped. Lower config.EARNINGS_CHUNK_DAYS.",
                    frm, to, config.EARNINGS_ROW_CAP, len(rows),
                )
            for r in rows:
                sym = r.get("symbol")
                date_str = r.get("date")
                if not sym or not date_str:
                    continue
                try:
                    d = dt.date.fromisoformat(date_str[:10])
                except ValueError:
                    continue
                if d < today:
                    continue
                iso = d.isoformat()
                if sym not in mapping or iso < mapping[sym]:
                    mapping[sym] = iso

        if not any_ok:
            logger.error(
                "Earnings calendar completely unavailable -- the earnings-exclusion "
                "filter is INACTIVE for this scan"
            )
            return mapping

        near = sum(
            1
            for v in mapping.values()
            if (dt.date.fromisoformat(v) - today).days <= config.EARNINGS_EXCLUSION_DAYS
        )
        logger.info(
            "Earnings calendar loaded for %s symbols (%s reporting within %s days)",
            len(mapping),
            near,
            config.EARNINGS_EXCLUSION_DAYS,
        )
        return mapping

    # ---------------------------------------------------------------- #
    # Trading-day calendar
    # ---------------------------------------------------------------- #
    async def is_trading_day(self, day: dt.date) -> bool:
        """
        True if `day` is a regular exchange trading day. Weekends are always False;
        weekdays are True unless the exchange holiday calendar marks the date closed.
        Fails open (returns True) on API error, so a transient failure never silently
        suppresses a scan.
        """
        if day.weekday() >= 5:  # Saturday / Sunday
            return False
        # The holiday endpoint needs a real date span (a single-day from==to range
        # returns nothing), so query a window around the day and match the date.
        rows = await self._get(
            "holidays-by-exchange",
            {
                "exchange": config.MARKET_EXCHANGE,
                "from": (day - dt.timedelta(days=7)).isoformat(),
                "to": (day + dt.timedelta(days=7)).isoformat(),
            },
        )
        if isinstance(rows, list):
            for r in rows:
                if r.get("date") == day.isoformat() and r.get("isClosed"):
                    return False
        return True

    # ---------------------------------------------------------------- #
    # Price history + indicators
    # ---------------------------------------------------------------- #
    async def _daily_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        # Stable API replaced the legacy `timeseries` bar count with a date
        # window and now returns a flat array (no enclosing {"historical": [...]}).
        # Pull a generous calendar window that clears DAILY_LOOKBACK_BARS trading days.
        frm = dt.date.today() - dt.timedelta(days=int(config.DAILY_LOOKBACK_BARS * 1.7))
        data = await self._get(
            "historical-price-eod/full",
            {"symbol": symbol, "from": frm.isoformat()},
        )
        if not isinstance(data, list) or not data:
            return None
        df = pd.DataFrame(data)
        needed = {"date", "open", "high", "low", "close", "volume"}
        if not needed.issubset(df.columns):
            return None
        # Own the frame outright before mutating a column. Under pandas
        # Copy-on-Write (default in pandas 3.0) writing into a sliced view can
        # silently no-op / corrupt the frame; an explicit copy makes the write
        # authoritative and clears the ChainedAssignment warning.
        df = df[["date", "open", "high", "low", "close", "volume"]]
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df

    async def _hourly_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Recent 1-hour candles, trimmed to HOURLY_LOOKBACK_DAYS.

        The endpoint returns ~3 months unasked (~427 bars for SPY); the cutoff trims
        that to ~8 sessions. Timestamps are naive and in EXCHANGE time, and the feed
        carries regular-hours bars only (09:30..15:30 -- verified against the live
        endpoint), so no premarket filter is needed: an hourly VWAP here is already a
        regular-session VWAP.

        The `date` column is deliberately kept: it is the only clock in this pipeline
        that comes from the exchange rather than from the machine running the scan
        (which is UTC on the CI runner). See `_market_today`.
        """
        data = await self._get("historical-chart/1hour", {"symbol": symbol})
        if not isinstance(data, list) or not data:
            return None
        df = pd.DataFrame(data)
        if "date" not in df.columns or "close" not in df.columns:
            return None
        # Own the frame before mutating (see _daily_frame): avoids CoW
        # chained-assignment corruption on the pandas 3.x build.
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=config.HOURLY_LOOKBACK_DAYS)
        df = df[df["date"] >= cutoff].sort_values("date").reset_index(drop=True)
        return df

    @staticmethod
    def _weekly_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
        # Aggregate daily -> weekly (week ending Friday) via groupby-on-period.
        # This deliberately avoids DataFrame.resample().agg(), which can crash
        # natively on some numpy/pandas build combinations (e.g. Python 3.14).
        s = daily.set_index("date").sort_index()
        week_key = s.index.to_period("W-FRI")
        w = (
            s.groupby(week_key)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )
        # Convert the weekly PeriodIndex back to the week-ending timestamp.
        w.index = w.index.to_timestamp(how="end").normalize()

        # DROP THE IN-PROGRESS WEEK. groupby buckets the current partial week into a
        # group of its own and aggregates it as though it had closed -- on a Monday
        # scan the newest "weekly bar" is a single day, and its label is dated to the
        # coming Friday (a date in the future). Weekly RSI / EMA21 / above_ema21 were
        # being read off that stub and handed to the model as a *weekly* trend read,
        # so one soft Monday morning could flip the weekly trend. A week is only
        # complete once the daily data reaches its Friday.
        last_daily = s.index[-1].normalize()
        if len(w) and w.index[-1] > last_daily:
            w = w.iloc[:-1]

        return w.tail(config.WEEKLY_LOOKBACK_WEEKS + 30)  # buffer to seed EMAs

    async def relative_volume(self, symbol: str) -> Optional[float]:
        """
        Today's volume divided by its recent average -- "is this name actually doing
        something today?".

        This is the rotation signal for universe selection. Ranking by raw dollar
        volume just ranks by market cap, and mega-cap dollar volume barely moves day
        to day, so the scanned list was a fixed set of the same ~60 giants forever.
        Relative volume is what surfaces the mid-cap breaking out on 5x its normal
        turnover -- the setup this scanner exists to find.

        Deliberately fetches a SHORT window (not the 700-bar frame used for
        indicators): this runs across a wide pre-screen pool, and only the survivors
        pay for the full history.
        """
        frm = dt.date.today() - dt.timedelta(days=config.RELVOL_FETCH_DAYS)
        data = await self._get(
            "historical-price-eod/full", {"symbol": symbol, "from": frm.isoformat()}
        )
        if not isinstance(data, list) or len(data) < config.RELVOL_LOOKBACK_BARS + 1:
            return None
        try:
            df = pd.DataFrame(data)[["date", "volume"]].copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            vols = df["volume"].astype(float)
            today_vol = float(vols.iloc[-1])
            # Average of the PRIOR bars -- excluding today, or today's own volume
            # would dilute the very spike we're trying to detect.
            baseline = float(vols.iloc[-(config.RELVOL_LOOKBACK_BARS + 1) : -1].mean())
        except (KeyError, ValueError, IndexError):
            return None
        if not baseline or baseline <= 0:
            return None
        return round(today_vol / baseline, 2)

    async def get_ticker_data(self, symbol: str) -> Optional[dict]:
        """
        Fetch and compute the full FMP feature set for one symbol. Returns a dict
        with daily/weekly/hourly derived features, or None on hard failure.
        """
        try:
            daily = await self._daily_frame(symbol)
            if daily is None or len(daily) < 30:
                logger.warning("Insufficient daily data for %s; skipping", symbol)
                return None

            hourly = await self._hourly_frame(symbol)

            close = daily["close"]
            ema8, ema21, ema50, ema200 = (_ema(close, s) for s in (8, 21, 50, 200))
            rsi14 = _rsi(close, 14)
            macd_line, signal_line, hist = _macd(close)
            atr14 = _atr(daily, 14)

            weekly = self._weekly_from_daily(daily)
            w_close = weekly["close"]
            w_rsi = _rsi(w_close, 14) if len(w_close) >= 20 else pd.Series([np.nan])
            w_ema8 = _ema(w_close, 8) if len(w_close) >= 8 else pd.Series([np.nan])
            w_ema21 = _ema(w_close, 21) if len(w_close) >= 21 else pd.Series([np.nan])

            last_close = float(close.iloc[-1])
            e8, e21, e50, e200 = (
                float(ema8.iloc[-1]),
                float(ema21.iloc[-1]),
                float(ema50.iloc[-1]),
                float(ema200.iloc[-1]),
            )
            # Market-structure booleans. An EMA with too little history is NaN (see
            # _ema), and every comparison against NaN is False -- which would report
            # "not above the 200 EMA" for a ticker that simply has no 200 EMA yet,
            # making "unknown" indistinguishable from "bearish". Emit None instead.
            emas_known = not any(np.isnan(v) for v in (e8, e21, e50, e200))
            ema_stack_bullish = (e8 > e21 > e50 > e200) if emas_known else None
            price_above_200 = (last_close > e200) if not np.isnan(e200) else None

            window20 = daily.tail(20)
            swing_high = float(window20["high"].max())
            swing_low = float(window20["low"].min())

            # --- Volume expansion + base tightness ------------------------- #
            # Relative volume (current bar vs its own trailing-20 average) is the
            # single most important breakout signal, and the payload carried no
            # volume field at all -- so surface it here from the daily frame we
            # already hold (no extra fetch). The baseline EXCLUDES the current bar
            # for the same reason relative_volume() does: including today dilutes
            # the very spike we're trying to measure. Note the current bar is the
            # PRIOR completed session at premarket and today's PARTIAL bar at the
            # pulse, so rel_volume means "did it break on volume yesterday" before
            # the open and "is it breaking on volume now" intraday -- the model
            # reads it alongside `macro.day_progress.as_of`.
            vols = daily["volume"].astype(float)
            today_vol = float(vols.iloc[-1])
            vol_baseline = (
                float(vols.iloc[-21:-1].mean()) if len(vols) >= 21 else None
            )
            rel_volume = (
                _round(today_vol / vol_baseline, 2)
                if vol_baseline and vol_baseline > 0
                else None
            )

            # ATR% now vs ~10 bars ago: a tight, coiling base is one whose
            # volatility is CONTRACTING into the break. One extra scalar lets the
            # breakout rubric reward a declining ATR% without a full series.
            atr_pct_series = (atr14 / close) * 100.0
            atr_pct_10d_ago = (
                _round(atr_pct_series.iloc[-11]) if len(atr_pct_series) > 11 else None
            )

            # relative-strength inputs (raw returns; vs-SPY computed in aggregator)
            def _ret(n: int) -> Optional[float]:
                if len(close) <= n:
                    return None
                return _round((last_close / float(close.iloc[-1 - n]) - 1.0) * 100.0)

            last_hour_close = (
                float(hourly["close"].iloc[-1]) if hourly is not None and len(hourly) else None
            )

            # --- Intraday / day-progression -------------------------------- #
            # FMP's newest DAILY bar updates live during the session, so at the
            # midday pulse it is today's PARTIAL bar: today's open, today's running
            # high/low, and a live close. That is a full day-progression read the
            # scanner was already paying for and discarding.
            #
            # Whether that is true right now is a question about the data, never
            # about the session name -- `--force`, a manual run or a late dispatch
            # all detach the session label from reality. Compare the bar's own date
            # to the exchange's date (_market_today).
            market_today = _market_today(hourly)
            last_bar_date = daily["date"].iloc[-1].date()
            bar_is_today = (
                bool(last_bar_date == market_today) if market_today is not None else None
            )

            today_open = float(daily["open"].iloc[-1])
            today_high = float(daily["high"].iloc[-1])
            today_low = float(daily["low"].iloc[-1])

            # Where in the day's range price sits: 0 = on the lows, 100 = on the
            # highs. One scalar that says "closing on the lows" without a chart.
            # A zero-width range (a halt, or a bar with a single print) has no
            # position to report -- None, not a fabricated 50.
            day_span = today_high - today_low
            range_position = (
                ((last_close - today_low) / day_span) * 100.0 if day_span > 0 else None
            )
            ret_from_open = (
                (last_close / today_open - 1.0) * 100.0 if today_open > 0 else None
            )

            hourly_today = (
                hourly[hourly["date"].dt.date == market_today]
                if hourly is not None and len(hourly) and market_today is not None
                else None
            )

            avwap_10d = _vwap(hourly)
            session_vwap = _vwap(hourly_today)

            struct = structure.build(daily, weekly)

            # Where this setup is worth buying (see data/entry_zone.py). Uses the
            # frames and levels already computed above -- no extra API calls.
            zone = entry_zone.build(
                daily,
                struct,
                price=last_close,
                atr14=_round(atr14.iloc[-1]),
                emas={"ema8": _round(e8), "ema21": _round(e21), "ema50": _round(e50)},
                weekly_ema21=_round(w_ema21.iloc[-1]) if len(w_ema21) else None,
                avwap_10d=avwap_10d,
                session_vwap=session_vwap,
            )

            return {
                "symbol": symbol,
                "price": _round(last_close),
                "daily": {
                    "close": _round(last_close),
                    "rsi14": _round(rsi14.iloc[-1]),
                    "macd": _round(macd_line.iloc[-1], 3),
                    "macd_signal": _round(signal_line.iloc[-1], 3),
                    "macd_hist": _round(hist.iloc[-1], 3),
                    "atr14": _round(atr14.iloc[-1]),
                    "atr_pct": _round((atr14.iloc[-1] / last_close) * 100.0),
                    # ATR% ~10 bars ago -- below atr_pct now means volatility is
                    # contracting into a tight base (a breakout-rubric input).
                    "atr_pct_10d_ago": atr_pct_10d_ago,
                    # Current-bar volume and its expansion over the trailing-20
                    # average -- the breakout rubric's volume-confirmation gate.
                    "volume": int(today_vol) if today_vol == today_vol else None,
                    "rel_volume": rel_volume,
                    "ema8": _round(e8),
                    "ema21": _round(e21),
                    "ema50": _round(e50),
                    "ema200": _round(e200),
                    "ema_stack_bullish": ema_stack_bullish,
                    "price_above_ema200": price_above_200,
                    "dist_to_ema200_pct": _round((last_close / e200 - 1.0) * 100.0),
                    "swing_high_20d": _round(swing_high),
                    "swing_low_20d": _round(swing_low),
                    "last_10_closes": [_round(c) for c in close.tail(10).tolist()],
                    # When bar_is_today is False these describe the PRIOR session --
                    # still useful, but they mean something different. Emit the raw
                    # numbers plus the flag and let the aggregator decide; do not
                    # bake the interpretation in here.
                    "bar_is_today": bar_is_today,
                    "today_open": _round(today_open),
                    "today_high": _round(today_high),
                    "today_low": _round(today_low),
                    "ret_from_open_pct": _round(ret_from_open),
                    "range_position_pct": _round(range_position, 1),
                },
                "weekly": {
                    "close": _round(w_close.iloc[-1]) if len(w_close) else None,
                    "rsi14": _round(w_rsi.iloc[-1]) if len(w_rsi) else None,
                    "ema8": _round(w_ema8.iloc[-1]) if len(w_ema8) else None,
                    "ema21": _round(w_ema21.iloc[-1]) if len(w_ema21) else None,
                    # None (not False) when the weekly EMA21 doesn't exist yet --
                    # "unknown" must not read as "price broke below the weekly EMA".
                    "above_ema21": (
                        bool(w_close.iloc[-1] > w_ema21.iloc[-1])
                        if len(w_ema21) and len(w_close) and not np.isnan(w_ema21.iloc[-1])
                        else None
                    ),
                },
                # Swing structure from highs/lows (see data/structure.py). Uses the
                # frames already in hand -- no extra API calls.
                "structure": struct,
                "entry_zone": zone,
                "hourly": {
                    "last_close": _round(last_hour_close),
                    "bars": int(len(hourly)) if hourly is not None else 0,
                    # Anchored VWAP over the whole hourly window (~8 sessions): a
                    # real, volume-weighted level for the entry zone to cluster on.
                    "avwap_10d": _round(avwap_10d),
                    # Today's VWAP only. None before the open (no bars today), which
                    # is the honest premarket answer -- entry_zone simply drops the
                    # level rather than substituting the 10-day one for it.
                    "session_vwap": _round(session_vwap),
                    "bars_today": int(len(hourly_today)) if hourly_today is not None else 0,
                },
                "returns": {
                    "ret5": _ret(5),
                    "ret20": _ret(20),
                },
                # closes kept for the aggregator's relative-strength calc
                "_daily_closes": {
                    row.date.strftime("%Y-%m-%d"): float(row.close)
                    for row in daily.tail(30).itertuples()
                },
            }
        except Exception as exc:  # defensive: never let one symbol kill the scan
            logger.exception("Unexpected error building FMP data for %s: %s", symbol, exc)
            return None

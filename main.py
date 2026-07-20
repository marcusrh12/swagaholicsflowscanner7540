"""
FlowScanner entry point.

Orchestrates one full scan session:
  screen universe -> filter earnings -> fetch FMP + Unusual Whales + VIX ->
  aggregate -> Claude confluence analysis -> render HTML.

Run modes:
  python main.py --run now     # fire one scan immediately (for testing / OS scheduler)
  python main.py               # start the built-in scheduler daemon (three sessions/day)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import re
import sys

import aiohttp

import config
import scheduler
from analysis.claude_engine import ClaudeEngine
from data import aggregator, sector_etf
from data.fmp import FMPClient
from data.unusual_whales import UnusualWhalesClient
from data.vix import get_vix
from output.publisher import publish_to_github

logger = logging.getLogger("flowscanner.main")


# --------------------------------------------------------------------------- #
# Day-over-day streak tracking
# --------------------------------------------------------------------------- #
# Pull "TICKER ... N confluences" straight from a rendered history card head.
_HISTORY_CARD_RE = re.compile(
    r'<div class="ticker">([A-Za-z][A-Za-z0-9.\-]*)<small>.*?</small></div>'
    r'.*?<span class="badge count">(\d+)\s*confluences?</span>',
    re.DOTALL,
)


def _session_stamp(path) -> str:
    """
    Sort key for a history file: the '<YYYYmmdd>_<HHMMSS>' stamp from its stem.
    Zero-padded, so lexicographic order IS chronological order.

    Do NOT sort these by st_mtime. On the GitHub Actions runner the repo is freshly
    cloned, so every file's mtime is checkout time -- git writes the tree in index
    (alphabetical) order, which makes mtime order collapse to *filename* order. With
    session-prefixed names that sorts by session type, not by date: the last-3 window
    silently fills with three 'pulse' files ('premarket' < 'pulse') and the premarket
    sessions are never read at all. The bug is invisible locally, where mtimes are real.
    """
    stem = path.stem
    _, _, stamp = stem.partition("_")
    return stamp or stem


def _recent_history_files(n: int = config.HISTORY_SESSIONS) -> list:
    """The last `n` timestamped history HTML files, oldest -> newest chronologically."""
    try:
        files = sorted(config.HISTORY_DIR.glob("*.html"), key=_session_stamp)
    except OSError:
        return []
    return files[-n:]


def _is_error_page(html: str) -> bool:
    """
    True if this archived session is a SCANNER FAILURE page, not a real session.

    A failed Claude call renders an error page with zero cards. Counting it as a
    session breaks every live streak: the ticker is absent from the '1 session ago'
    slot, so _compute_streaks sees a gap and reports no streak -- one transient API
    error silently erases days of streak state. A genuinely quiet tape (0 cards, no
    error) is a real session and *should* break streaks; only failures are skipped.
    """
    return '<div class="error">' in html


def _readable_history_files(n: int) -> list[tuple[str, str]]:
    """(stem, html) for the last `n` real sessions, oldest -> newest, errors skipped."""
    out: list[tuple[str, str]] = []
    # Walk newest-first so skipped error pages are backfilled with older real sessions.
    for path in reversed(_recent_history_files(config.HISTORY_SCAN_DEPTH)):
        try:
            html = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _is_error_page(html):
            logger.info("Skipping error page in streak history: %s", path.name)
            continue
        out.append((path.stem, html))
        if len(out) >= n:
            break
    return list(reversed(out))


def load_recent_history(n: int = config.HISTORY_SESSIONS) -> dict[str, list[tuple[str, int]]]:
    """
    Read the last `n` real (non-error) history files, chronologically, and extract per
    ticker the confluence count it showed in each session it appeared in. Returns
    ticker -> [(session_stem, confluence_count)], ordered oldest -> newest.
    Fault-tolerant: an unreadable file is skipped rather than raising.

    Windowed purely by recency (timestamp), NEVER filtered by day or session type. That
    is what lets the 10:15 confirmation scan see THIS MORNING's 09:00 premarket scan as
    the most recent prior session ("1 session ago"), so Claude can compare a setup's
    premarket read against how it is actually trading now. On GitHub Actions the premarket
    run commits its history file and the confirmation run git-pulls it before scanning, so
    the same-day earlier session is on disk by the time this reads it.
    """
    history: dict[str, list[tuple[str, int]]] = {}
    for stem, html in _readable_history_files(n):
        seen: set[str] = set()
        for m in _HISTORY_CARD_RE.finditer(html):
            sym = m.group(1).upper()
            if sym in seen:
                continue  # one entry per ticker per session
            seen.add(sym)
            history.setdefault(sym, []).append((stem, int(m.group(2))))
    return history


def _sessions_ago_map(n: int = config.HISTORY_SESSIONS) -> dict[str, int]:
    """Map each recent session stem -> how many sessions ago it was (newest = 1)."""
    stems = [stem for stem, _ in _readable_history_files(n)]
    k = len(stems)
    return {stem: k - i for i, stem in enumerate(stems)}


def _repeat_history_for_prompt(
    history: dict[str, list[tuple[str, int]]], ago_map: dict[str, int]
) -> dict[str, list[tuple[int, int]]]:
    """ticker -> [(sessions_ago, confluences)] ordered oldest (largest ago) -> newest."""
    out: dict[str, list[tuple[int, int]]] = {}
    for ticker, appts in history.items():
        annotated = [(ago_map[stem], cnt) for stem, cnt in appts if stem in ago_map]
        if annotated:
            out[ticker] = sorted(annotated, reverse=True)  # "2 ago", then "1 ago"
    return out


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _compute_streaks(
    cards: list[dict], repeat_history: dict[str, list[tuple[int, int]]]
) -> dict[str, dict]:
    """
    For each current-session card, compute the consecutive-session streak (including
    this session) and whether confluence is strengthening / stable / weakening. Only
    tickers on a live streak of >= 2 consecutive sessions are returned. A prior
    appearance that is NOT in the immediately preceding session does not count (the
    streak must be unbroken through "1 session ago").
    """
    streaks: dict[str, dict] = {}
    for card in cards:
        ticker = str(card.get("ticker", "")).upper()
        appts = repeat_history.get(ticker)
        if not appts:
            continue
        by_ago = {ago: cnt for ago, cnt in appts}
        consecutive = 0
        a = 1
        while a in by_ago:
            consecutive += 1
            a += 1
        if consecutive < 1:
            continue  # appeared before, but not last session -> streak is broken
        hits = consecutive + 1  # include the current session
        current = card.get("confluence_count") or 0
        oldest_in_run = by_ago[consecutive]  # confluence at the start of the run
        if current > oldest_in_run:
            trend = "strengthening"
        elif current < oldest_in_run:
            trend = "weakening"
        else:
            trend = "stable"
        streaks[ticker] = {"hits": hits, "trend": trend, "ordinal": _ordinal(hits)}
    return streaks


# --------------------------------------------------------------------------- #
# Universe selection
# --------------------------------------------------------------------------- #
def _dollar_vol(row: dict) -> float:
    try:
        return float(row.get("price", 0)) * float(row.get("volume", 0))
    except (TypeError, ValueError):
        return 0.0


def _take_capped(rows: list[dict], total: int, per_sector: int) -> list[dict]:
    """
    Take up to `total` rows, capping how many share a sector ETF at `per_sector` so a
    single hot group can't monopolize the leadership tail. `rows` must already be sorted
    in the desired priority order (most-liquid first); this only enforces the caps.
    """
    picked: list[dict] = []
    per_etf: dict[str, int] = {}
    for r in rows:
        if len(picked) >= total:
            break
        etf = sector_etf.etf_for(r.get("sector"), r.get("industry"))
        if per_etf.get(etf, 0) >= per_sector:
            continue
        per_etf[etf] = per_etf.get(etf, 0) + 1
        picked.append(r)
    return picked


async def _select_universe(
    fmp: FMPClient,
    screener_rows: list[dict],
    earnings_map: dict[str, str],
    sector_etf_data: dict | None = None,
    spy_returns: dict | None = None,
) -> list[dict]:
    """
    Build the scan universe as a STABLE CORE plus a ROTATING TAIL plus a LEADERSHIP TAIL.

    Ranking purely by dollar volume (the old behaviour) is very nearly ranking by
    market cap: mega-cap dollar volume is stable day to day, so the scanned list was
    a fixed set of the same ~60 giants every session, and the entire $1-50B band --
    where a stock can actually move 15% in three weeks and make a call worth owning --
    was screened in and then thrown away.

    Pure relative-volume ranking would fix discovery but break the streak tracking:
    streaks only mean something if a name can recur across sessions, and a fully
    churning list makes that impossible. So:

      * CORE   -- top N by dollar volume. The names you always want watched, and
                  where day-over-day streaks accumulate.
      * ROTATE -- top N by RELATIVE volume (today vs its own 20-day average) among
                  liquid names. This is what surfaces the mid-cap breaking out on 5x
                  its normal turnover.
      * LEADERSHIP -- a few names from sectors just STARTING TO TURN UP (their ETF is
                  accelerating -- see sector_etf.sector_improving), even when they are
                  neither top-dollar-volume nor high-relvol. This catches early sector
                  rotation the other two tails miss. Discovery-only, like ROTATE: it
                  never anchors streaks.

    Relative volume needs a volume history FMP's screener doesn't carry, so it is
    computed only for a pre-screen pool (ranked by turnover, a zero-cost proxy for
    unusual activity relative to size), not for all ~1,500 names.
    """
    today = dt.date.today()
    eligible: list[dict] = []
    for r in screener_rows:
        sym = r.get("symbol")
        if not sym or sym in config.MACRO_TICKERS:
            continue
        e_date = earnings_map.get(sym)
        if e_date:
            try:
                days = (dt.date.fromisoformat(e_date) - today).days
                if 0 <= days <= config.EARNINGS_EXCLUSION_DAYS:
                    continue  # exclude imminent earnings
            except ValueError:
                pass
        eligible.append(r)

    by_dollar = sorted(eligible, key=_dollar_vol, reverse=True)
    core = by_dollar[: config.UNIVERSE_CORE_SLOTS]
    core_syms = {r["symbol"] for r in core}

    # Pre-screen pool for the rotating tail: the next most liquid names after the
    # core. The pool selects for LIQUIDITY (a name whose options you can actually get
    # filled on) and lets relative volume do the DISCOVERING. Ranking the pool itself
    # by turnover instead pulls in perennially-churny micro-caps whose chains are too
    # thin to trade -- they'd only be dropped later by the open-interest filter.
    pool = [
        r
        for r in eligible
        if r["symbol"] not in core_syms
        and _dollar_vol(r) >= config.MIN_DOLLAR_VOLUME
    ]
    pool.sort(key=_dollar_vol, reverse=True)
    pool = pool[: config.UNIVERSE_PRESCREEN_POOL]

    relvols = await asyncio.gather(
        *[fmp.relative_volume(r["symbol"]) for r in pool]
    )
    scored = [
        (r, rv) for r, rv in zip(pool, relvols) if rv is not None and rv >= config.MIN_RELVOL
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    rotate = [r for r, _ in scored[: config.UNIVERSE_ROTATE_SLOTS]]

    if rotate:
        logger.info(
            "Rotating tail (relvol): %s",
            ", ".join(f"{r['symbol']}={rv}x" for r, rv in scored[: config.UNIVERSE_ROTATE_SLOTS]),
        )
    else:
        logger.warning(
            "No ticker cleared the %sx relative-volume floor; scanning the core only",
            config.MIN_RELVOL,
        )

    # LEADERSHIP tail: names from sectors just starting to turn up. Ranked by SECTOR
    # momentum (not the name's own volume), so it catches early rotation the dollar-
    # volume/relvol tails miss -- e.g. a semis name when SMH is accelerating out of a
    # base while the group still looks weak on 20 days. Discovery-only.
    leadership: list[dict] = []
    sector_scores: list[tuple[str, float]] = []
    already = core_syms | {r["symbol"] for r in rotate}
    if sector_etf_data:
        spy_rets = spy_returns or {}
        # Rank the sectors present in the eligible set by how strongly they are improving.
        candidate_etfs = {
            sector_etf.etf_for(r.get("sector"), r.get("industry")) for r in eligible
        } - {None}
        for etf in sorted(candidate_etfs):
            ok, accel = sector_etf.sector_improving(sector_etf_data.get(etf), spy_rets)
            if ok:
                sector_scores.append((etf, accel))
        sector_scores.sort(key=lambda x: x[1], reverse=True)
        lead_etfs = {e for e, _ in sector_scores[: config.LEADERSHIP_SECTOR_COUNT]}

        # From those sectors, take the most-liquid names not already selected. Liquidity
        # ordering (same rationale as the ROTATE pre-screen) selects for tradable chains.
        lead_pool = [
            r
            for r in eligible
            if r["symbol"] not in already
            and sector_etf.etf_for(r.get("sector"), r.get("industry")) in lead_etfs
            and _dollar_vol(r) >= config.MIN_DOLLAR_VOLUME
        ]
        lead_pool.sort(key=_dollar_vol, reverse=True)
        leadership = _take_capped(
            lead_pool, config.LEADERSHIP_SLOTS, config.LEADERSHIP_PER_SECTOR_MAX
        )

    if leadership:
        logger.info(
            "Leadership tail (improving sectors %s): %s",
            ", ".join(
                f"{e}(+{a})" for e, a in sector_scores[: config.LEADERSHIP_SECTOR_COUNT]
            ),
            ", ".join(r["symbol"] for r in leadership),
        )
    elif sector_etf_data and not sector_scores:
        logger.info("Leadership tail: no sector cleared the improving-momentum test")

    selected = core + rotate + leadership
    if len(selected) < config.TARGET_MIN_TICKERS:
        logger.warning(
            "Post-filter universe is %s (< target floor %s); proceeding anyway",
            len(selected),
            config.TARGET_MIN_TICKERS,
        )
    logger.info(
        "Selected %s tickers (%s core by dollar volume + %s rotating by relative "
        "volume + %s leadership by sector momentum)",
        len(selected),
        len(core),
        len(rotate),
        len(leadership),
    )
    return selected


# --------------------------------------------------------------------------- #
# Per-ticker fetch
# --------------------------------------------------------------------------- #
async def _fetch_one(
    fmp: FMPClient,
    uw: UnusualWhalesClient,
    symbol: str,
) -> tuple[str, dict | None, dict]:
    # FMP first: the UW chain needs the spot price to filter to near-the-money
    # calls and compute their deltas, so these can't be fetched concurrently.
    # Tickers still run in parallel, so the cost is one round-trip, not a serial scan.
    fmp_data = await fmp.get_ticker_data(symbol)
    spot = (fmp_data or {}).get("price")
    uw_data = await uw.get_options_data(symbol, spot=spot)
    return symbol, fmp_data, uw_data


# --------------------------------------------------------------------------- #
# Full scan
# --------------------------------------------------------------------------- #
async def run_scan(session_name: str, force: bool = False) -> None:
    logger.info("=== Starting '%s' scan ===", session_name)
    started = dt.datetime.now()

    timeout = aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_SECONDS)
    semaphore = asyncio.Semaphore(config.HTTP_MAX_CONCURRENCY)

    async with aiohttp.ClientSession(timeout=timeout) as http:
        fmp = FMPClient(http, semaphore)
        uw = UnusualWhalesClient(http, semaphore)

        # Skip weekends and exchange holidays (unless explicitly forced).
        if config.SKIP_NON_TRADING_DAYS and not force:
            today = dt.date.today()
            if not await fmp.is_trading_day(today):
                logger.info(
                    "=== %s is not a trading day; skipping '%s' scan ===",
                    today.isoformat(),
                    session_name,
                )
                return

        # Macro FMP data + screener + earnings + VIX + the sector ETFs, all in parallel.
        # The sector ETFs are a FIXED ~12-symbol set (sector_etf.all_etfs()) and are
        # fetched HERE -- before universe selection -- because the LEADERSHIP tail ranks
        # sectors by their ETF momentum at selection time. This is the SAME fetch that
        # later feeds each ticker's sector_context, so we do it once up front and reuse
        # it: net-zero API calls versus the old post-selection fetch, just moved earlier.
        macro_tasks = {sym: fmp.get_ticker_data(sym) for sym in config.MACRO_TICKERS}
        etf_tasks = {e: fmp.get_ticker_data(e) for e in sorted(sector_etf.all_etfs())}
        (
            screener_rows,
            earnings_map,
            vix_data,
            *rest,
        ) = await asyncio.gather(
            fmp.screen_universe(),
            fmp.earnings_map(),
            get_vix(http),
            *macro_tasks.values(),
            *etf_tasks.values(),
        )
        macro_fmp = dict(zip(macro_tasks.keys(), rest[: len(macro_tasks)]))
        sector_etf_data = dict(zip(etf_tasks.keys(), rest[len(macro_tasks):]))

        # SPY returns for relative-strength math (sector-vs-SPY, leadership ranking).
        # Needed BEFORE selection now that the leadership tail uses it.
        spy_data = macro_fmp.get("SPY") or {}
        spy_returns = spy_data.get("returns", {}) if spy_data else {}

        # Build the tradable universe. Needs the client (the rotating tail is ranked on
        # relative volume, which requires a volume history per candidate) and the sector
        # ETF data (the leadership tail is ranked on sector momentum).
        universe = await _select_universe(
            fmp,
            screener_rows,
            earnings_map,
            sector_etf_data=sector_etf_data,
            spy_returns=spy_returns,
        )
        screener_by_symbol = {r["symbol"]: r for r in universe}
        symbols = list(screener_by_symbol.keys())

        # Resolve each scanned name to its sector ETF. The ETF DATA is already in hand
        # from the macro gather above; this map just records which ETF each name uses so
        # aggregation can attach the right sector_context.
        etf_by_symbol = {
            sym: sector_etf.etf_for(row.get("sector"), row.get("industry"))
            for sym, row in screener_by_symbol.items()
        }

        # Per-ticker data (sector-ETF data already fetched; nothing else to gather here).
        results = await asyncio.gather(*[_fetch_one(fmp, uw, sym) for sym in symbols])

    # Assemble per-ticker records (skip symbols with no usable FMP data).
    records: list[dict] = []
    for symbol, fmp_data, uw_data in results:
        if fmp_data is None:
            logger.info("Skipping %s (no usable price data)", symbol)
            continue
        etf_symbol = etf_by_symbol.get(symbol)
        record = aggregator.build_ticker_record(
            fmp_data=fmp_data,
            uw_data=uw_data,
            earnings_date=earnings_map.get(symbol),
            spy_returns=spy_returns,
            screener_row=screener_by_symbol.get(symbol),
            sector_etf_symbol=etf_symbol,
            sector_etf_data=sector_etf_data.get(etf_symbol) if etf_symbol else None,
        )
        records.append(record)

    tickers_scanned = len(records)
    logger.info("Assembled %s ticker records", tickers_scanned)

    macro = aggregator.build_macro_context(macro_fmp, vix_data)
    # How the session is actually trading, plus real breadth across everything just
    # scanned. Attached to `macro` rather than threaded separately: `macro` already
    # reaches both the model (assemble_payload) and the page (renderer.render), so
    # this needs no new call sites. `records` is built above, so ordering works.
    breadth = aggregator.build_breadth(records)
    macro["day_progress"] = aggregator.build_day_progress(macro_fmp, breadth, vix_data)
    logger.info(
        "Tape: %s (as_of=%s, %s%% green, universe %s)",
        macro["day_progress"].get("tape"),
        macro["day_progress"].get("as_of"),
        breadth.get("pct_green"),
        breadth.get("universe_size"),
    )

    payload = aggregator.assemble_payload(session_name, records, macro)

    # Day-over-day streak context: how recently/repeatedly each ticker has surfaced.
    history = load_recent_history()
    repeat_history = _repeat_history_for_prompt(history, _sessions_ago_map())
    payload["repeat_history"] = repeat_history  # consumed by prompt_builder.build_messages
    if repeat_history:
        logger.info("Repeat-ticker history spans %s prior ticker(s)", len(repeat_history))

    # Claude confluence analysis.
    if not records:
        logger.warning("No ticker records to analyze; rendering empty report")
        analysis = {
            "market_summary": "No data available this session.",
            "trade_cards": [],
            "breakout_cards": [],
            "error": None,
        }
    else:
        engine = ClaudeEngine()
        analysis = await engine.analyze(payload)

    # Streak badges for the renderer (based on this session's cards + prior history).
    streaks = _compute_streaks(analysis.get("trade_cards", []), repeat_history)
    if streaks:
        logger.info("Streak badges: %s", {k: v["hits"] for k, v in streaks.items()})

    # Render output (imported lazily so Jinja env picks up the template dir).
    from output import renderer

    out_path, html_content = renderer.render(
        session_name, macro, analysis, tickers_scanned, streaks
    )

    session_metadata = {
        "session_type": session_name,
        "date": dt.date.today().isoformat(),
        "setups": len(analysis.get("trade_cards", [])),
    }
    if config.ENABLE_GITHUB_PAGES:
        await publish_to_github(html_content, session_metadata)

    elapsed = (dt.datetime.now() - started).total_seconds()
    logger.info(
        "=== '%s' scan complete in %.1fs: %s cards, output -> %s ===",
        session_name,
        elapsed,
        len(analysis.get("trade_cards", [])),
        out_path,
    )


def _infer_session_name() -> str:
    """
    Session name for a manual run: SESSION_TYPE if set (GitHub Actions passes the
    cron-derived name), otherwise the configured session whose time is nearest now.
    """
    override = os.getenv("SESSION_TYPE", "").strip()
    if override:
        return override

    now = dt.datetime.now()
    best_name, best_delta = "manual", None
    for name, hhmm in config.SCAN_SESSIONS.items():
        hh, mm = map(int, hhmm.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = abs((now - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta, best_name = delta, name
    return best_name


def main() -> None:
    parser = argparse.ArgumentParser(description="FlowScanner options swing scanner")
    parser.add_argument(
        "--run",
        choices=["now"],
        help="Bypass the scheduler and fire one scan immediately.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even on weekends/holidays (bypass the trading-day skip).",
    )
    args = parser.parse_args()

    config.setup_logging()

    missing = config.validate_keys()
    if missing:
        logger.error("Missing API keys in .env: %s", ", ".join(missing))
        logger.error("Copy .env.example to .env and fill in your keys before running.")
        sys.exit(1)

    # aiohttp on Windows works best with the selector event loop.
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if args.run == "now":
        session = _infer_session_name()
        asyncio.run(run_scan(session, force=args.force))
    else:
        asyncio.run(scheduler.run_scheduler(run_scan))


if __name__ == "__main__":
    main()

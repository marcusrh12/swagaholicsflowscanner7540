"""
Central configuration for FlowScanner.

All tunable scan parameters live here so they are easy to edit in one place.
API keys are loaded from the .env file and are never hardcoded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
HISTORY_DIR = OUTPUT_DIR / "history"
LOGS_DIR = BASE_DIR / "logs"
LATEST_HTML = OUTPUT_DIR / "latest.html"

for _d in (OUTPUT_DIR, HISTORY_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# API keys (loaded from .env -- never hardcode)
# --------------------------------------------------------------------------- #
# Local runs read .env; on GitHub Actions no .env exists and the env vars are
# injected directly, so this is a no-op there.
_ENV_FILE = BASE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)

FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
UW_API_KEY = os.getenv("UW_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# --------------------------------------------------------------------------- #
# GitHub Pages publishing
# --------------------------------------------------------------------------- #
# GH_TOKEN is provided automatically by GitHub Actions; locally it comes from .env.
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()
REPO_OWNER = os.getenv("REPO_OWNER", "marcusrh12").strip()
REPO_NAME = os.getenv("REPO_NAME", "swagaholicsflowscanner7540").strip()
PAGES_URL = os.getenv(
    "PAGES_URL", "https://marcusrh12.github.io/swagaholicsflowscanner7540"
).strip()
ENABLE_GITHUB_PAGES = True

# --------------------------------------------------------------------------- #
# Claude model
# --------------------------------------------------------------------------- #
CLAUDE_MODEL = "claude-fable-5"
CLAUDE_FALLBACK_MODEL = "claude-opus-4-8"  # retried once on a fable-5 safety refusal
CLAUDE_EFFORT = "medium"             # fable-5 adaptive-thinking effort ("low"/"medium"/"high"); "" to omit
# max_tokens covers thinking AND the JSON output. Medium effort spends ~8-9k on
# thinking alone; the seven-category rubric then needs ~4-6k for the cards. 12000
# truncated the JSON mid-card (stop_reason=max_tokens), which surfaces as an
# unparseable response, so keep real headroom above thinking + output.
CLAUDE_MAX_TOKENS = 24000
CLAUDE_TIMEOUT_SECONDS = 180
CLAUDE_RETRY_DELAY_SECONDS = 10  # retry once after this delay on failure

# --------------------------------------------------------------------------- #
# Universe / filtering parameters (easy to edit)
# --------------------------------------------------------------------------- #
MIN_MARKET_CAP = 1_000_000_000        # > $1B
MIN_AVG_VOLUME = 500_000              # > 500K average volume
MIN_PRICE = 10.0                      # > $10
EXCHANGES = ["NYSE", "NASDAQ"]        # listed venues

SKIP_NON_TRADING_DAYS = True          # skip scans on weekends and exchange holidays
MARKET_EXCHANGE = "NASDAQ"            # exchange whose holiday calendar gates trading days

EARNINGS_EXCLUSION_DAYS = 16          # skip tickers reporting within N days
EARNINGS_LOOKAHEAD_DAYS = 90          # window used to find the nearest earnings date

# FMP's earnings-calendar endpoint caps a response at 4000 rows and truncates from
# the NEAR end, so one 90-day request silently returned only days 29-90 -- the
# exclusion window was empty and no reporter was ever filtered out. Fetch the window
# in chunks small enough that no single response can hit the cap.
# 14 still hit the cap during peak earnings season (a single 2-week chunk returned
# exactly 4000 rows). 7 keeps every chunk clear of it; the ERROR log above is the
# tripwire if a future season pushes past it again.
EARNINGS_CHUNK_DAYS = 7
EARNINGS_ROW_CAP = 4000               # if a chunk returns this many rows, it truncated

TARGET_MIN_TICKERS = 40               # desired post-filter universe size (floor)
TARGET_MAX_TICKERS = 60               # desired post-filter universe size (cap)

# --------------------------------------------------------------------------- #
# Universe composition: a stable core + a rotating tail
# --------------------------------------------------------------------------- #
# Ranking purely by dollar volume is nearly ranking by market cap, so the scanned
# list was the same ~60 mega-caps every session and the $1-50B band -- where a stock
# can actually move enough to pay for a call -- was never looked at. The core keeps
# continuity (streaks need names to recur); the tail brings discovery.
UNIVERSE_CORE_SLOTS = 30              # top N by dollar volume: always watched
UNIVERSE_ROTATE_SLOTS = 30            # top N by relative volume: actually moving today
UNIVERSE_PRESCREEN_POOL = 300         # candidates we compute relative volume for
MIN_DOLLAR_VOLUME = 100_000_000       # $/day floor for the tail -- options must be tradable
MIN_RELVOL = 1.5                      # tail must trade >= 1.5x its own 20-day average

RELVOL_LOOKBACK_BARS = 20             # baseline = mean volume of the prior 20 sessions
RELVOL_FETCH_DAYS = 45                # short calendar window -- enough bars, cheap to fetch

# Always pulled as macro context regardless of filters
MACRO_TICKERS = ["SPY", "QQQ"]

# --------------------------------------------------------------------------- #
# Data window parameters
# --------------------------------------------------------------------------- #
# EMA200 needs a long warm-up to converge: ewm(adjust=False) seeds off the first bar,
# and at ~293 bars roughly 5% of the value is still that seed. Measured against 5y of
# real prices, that skewed dist_to_ema200_pct by up to ~1.9 points. 700 bars drives the
# residual seed weight to ~0.1%. Costs nothing but a wider date range on the same call.
DAILY_LOOKBACK_BARS = 700
WEEKLY_LOOKBACK_WEEKS = 52
HOURLY_LOOKBACK_DAYS = 10

# A ticker needs at least this much daily history to be analyzable at all. Below 200
# bars there is no EMA200, hence no trend regime, which is the scanner's core gate.
MIN_DAILY_BARS = 200

# --------------------------------------------------------------------------- #
# Chart-structure parameters (data/structure.py)
# --------------------------------------------------------------------------- #
# Pivot width = bars required either side of a swing point to confirm it. Wider
# = fewer, more meaningful pivots. Weekly bars are coarser, so they need less.
PIVOT_WIDTH_DAILY = 3
PIVOT_WIDTH_WEEKLY = 2

# How far back structure is read. Deliberately shorter than the EMA lookbacks:
# a swing trader cares about the recent sequence, not the year-old one.
STRUCTURE_LOOKBACK_DAILY = 90         # bars (~4.5 months)
STRUCTURE_LOOKBACK_WEEKLY = 52        # bars (~1 year)

# A range is the longest recent window whose high/low width stays under this.
# Tighten to demand cleaner bases; loosen to catch wider ones.
CONSOLIDATION_MAX_WIDTH_PCT_DAILY = 8.0
CONSOLIDATION_MAX_WIDTH_PCT_WEEKLY = 15.0
CONSOLIDATION_MIN_BARS_DAILY = 8      # a range needs at least this many bars
CONSOLIDATION_MIN_BARS_WEEKLY = 4

# --------------------------------------------------------------------------- #
# Options / flow parameters
# --------------------------------------------------------------------------- #
MIN_FLOW_PREMIUM = 50_000             # unusual flow alerts must exceed this premium ($)
FLOW_LOOKBACK_DAYS = 5                # only consider flow from the past N days
FLOW_FETCH_LIMIT = 200                # alerts pulled per ticker (endpoint is newest-first)
FLOW_ALERTS_SHOWN = 10                # largest alerts surfaced to the model per ticker
TOP_OI_STRIKES = 5                    # number of top open-interest strikes to surface

# UW reports IV as a fraction (0.28 = 28%). Values above 1.0 are legitimate on
# high-vol names; this is only a tripwire for a scale change at the source.
IV_SANITY_MAX = 10.0                  # 1000% IV -- beyond this the field is suspect

# --------------------------------------------------------------------------- #
# Option-chain / contract selection (data/unusual_whales.py)
# --------------------------------------------------------------------------- #
# The chain is filtered down to a shortlist of REAL, tradable calls that the
# model then picks from -- rather than inventing a strike, expiry and delta that
# may not correspond to any listed contract.
CHAIN_FETCH_LIMIT = 500               # contracts pulled per ticker from UW
CHAIN_MIN_MONEYNESS = -0.05           # strike vs spot: 5% ITM ...
CHAIN_MAX_MONEYNESS = 0.10            # ... through 10% OTM
CHAIN_MIN_OPEN_INTEREST = 100         # skip illiquid strikes you can't get filled on
CHAIN_MAX_CANDIDATES = 12             # shortlist size handed to the model per ticker

# Used only for the Black-Scholes delta on candidate contracts. Delta is not very
# sensitive to this, so an approximate short-rate is fine.
RISK_FREE_RATE = 0.04

# --------------------------------------------------------------------------- #
# Analysis parameters
# --------------------------------------------------------------------------- #
# Minimum confluence points to generate a trade card. There are now seven
# categories (market structure was added), so 3 would be a looser gate than the
# 3-of-6 it replaced; 4-of-7 keeps the bar where it was.
MIN_CONFLUENCE_COUNT = 4
CONFLUENCE_CATEGORY_COUNT = 7         # kept in sync with the rubric in prompt_builder

# Reward/risk floors, enforced in claude_engine (not just asked for in the prompt).
# MIN_RR_RATIO is measured on the UNDERLYING -- it judges the thesis.
# MIN_OPTION_RR is measured on the CONTRACT (Black-Scholes, data/pricing.py) -- it
# judges the trade, and is the gate that actually matters when you're buying calls:
# risk is the premium you cannot recover at the stop, reward is the modeled value at
# the target, and theta is priced in. A great chart can still be a bad call.
MIN_RR_RATIO = 1.5
MIN_OPTION_RR = 1.5

# The thesis is assumed to play out partway through the contract's life rather than
# on expiration day -- a swing that needs every last day to work is not the trade you
# thought you took, and pricing at expiry would throw away the time value you still
# hold when the target prints. 0.5 = the move completes at the halfway mark.
OPTION_RR_TIME_FRACTION = 0.5

# Expiration selection window (days-to-expiration) for recommended contracts.
MIN_DTE = 7                          # hard floor: never recommend an expiration closer than this
MAX_DTE = 56                         # upper bound of the swing DTE window

# --------------------------------------------------------------------------- #
# Scan session times (local machine time, 24h "HH:MM")
#
# Only used by the local scheduler daemon (scheduler.py). The scans that actually
# run are scheduled in .github/workflows/scan.yml, pinned to Eastern time — keep
# the two in sync. The postmarket scan was dropped: the market is closed, so its
# cards could not be acted on until the next open, by which point the premarket
# scan supersedes them.
# --------------------------------------------------------------------------- #
SCAN_SESSIONS = {
    "premarket": "09:00",  # the plan: 30 min before the bell, off yesterday's completed bar
    "pulse": "14:00",      # did it hold: FMP's daily bar updates intraday, so this re-reads
                           # today's live bar past the lunch chop, with time left to act
}

# --------------------------------------------------------------------------- #
# Streak / repeat-ticker history
# --------------------------------------------------------------------------- #
# How many prior REAL sessions feed the streak badges and the repeat-ticker block.
# At 2 scans/day, 3 sessions was only 1.5 trading days -- too short to see a
# day-over-day streak at all. 6 covers ~3 trading days.
HISTORY_SESSIONS = 6
# How many archived files to look back through to find those 6 real sessions
# (scanner-error pages are skipped, so this needs headroom above HISTORY_SESSIONS).
HISTORY_SCAN_DEPTH = 20

# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
AUTO_REFRESH_SECONDS = 300            # HTML auto-refresh cadence (5 minutes)

# --------------------------------------------------------------------------- #
# External endpoints
# --------------------------------------------------------------------------- #
FMP_BASE = "https://financialmodelingprep.com/stable"
UW_BASE = "https://api.unusualwhales.com"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"

# Unusual Whales endpoint templates (adjust here if your plan exposes different paths)
UW_OPTIONS_VOLUME = "/api/stock/{ticker}/options-volume"
UW_OPTION_CONTRACTS = "/api/stock/{ticker}/option-contracts"
UW_FLOW_ALERTS = "/api/stock/{ticker}/flow-alerts"
UW_VOLATILITY_STATS = "/api/stock/{ticker}/volatility/stats"

# --------------------------------------------------------------------------- #
# Rate limiting (requests per minute per provider)
# --------------------------------------------------------------------------- #
FMP_RATE_PER_MIN = 280                # FMP Starter is ~300/min; keep headroom
UW_RATE_PER_MIN = 110                 # conservative default for UW
HTTP_MAX_CONCURRENCY = 12             # global cap on simultaneous outbound requests
HTTP_RETRIES = 2                      # per-request retry attempts on transient failure
HTTP_TIMEOUT_SECONDS = 30


class AsyncRateLimiter:
    """Simple async token-bucket limiter. Caps calls to `rate_per_min` per minute."""

    def __init__(self, rate_per_min: int):
        self.rate_per_min = max(1, rate_per_min)
        self._min_interval = 60.0 / self.rate_per_min
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._min_interval


def setup_logging() -> logging.Logger:
    """Configure a rotating file logger plus console output."""
    logger = logging.getLogger("flowscanner")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOGS_DIR / "flowscanner.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger


def validate_keys() -> list[str]:
    """Return a list of missing key names (empty if all present)."""
    missing = []
    if not FMP_API_KEY:
        missing.append("FMP_API_KEY")
    if not UW_API_KEY:
        missing.append("UW_API_KEY")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    return missing

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

TARGET_MIN_TICKERS = 40               # desired post-filter universe size (floor)
TARGET_MAX_TICKERS = 60               # desired post-filter universe size (cap)

# Always pulled as macro context regardless of filters
MACRO_TICKERS = ["SPY", "QQQ"]

# --------------------------------------------------------------------------- #
# Data window parameters
# --------------------------------------------------------------------------- #
DAILY_LOOKBACK_BARS = 250             # enough to seed EMA200 + 200 days of history
WEEKLY_LOOKBACK_WEEKS = 52
HOURLY_LOOKBACK_DAYS = 10

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
TOP_OI_STRIKES = 5                    # number of top open-interest strikes to surface

# --------------------------------------------------------------------------- #
# Analysis parameters
# --------------------------------------------------------------------------- #
# Minimum confluence points to generate a trade card. There are now seven
# categories (market structure was added), so 3 would be a looser gate than the
# 3-of-6 it replaced; 4-of-7 keeps the bar where it was.
MIN_CONFLUENCE_COUNT = 4
CONFLUENCE_CATEGORY_COUNT = 7         # kept in sync with the rubric in prompt_builder

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

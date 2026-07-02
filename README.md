# FlowScanner

A stock-options scanner that runs three times daily (premarket, midday, postmarket)
to identify high-probability swing **call** setups using multi-confluence analysis.

It screens a large-cap US-equity universe, pulls price/technical/options/flow data,
sends a single structured payload to **Claude (`claude-sonnet-4-6`)** for confluence
scoring, and renders clean, auto-refreshing HTML trade cards.

> ⚠️ **For research and educational use only. Not investment advice.** Options
> trading carries substantial risk. Verify every number before acting on it.

---

## What it does

Per session, FlowScanner:

1. **Screens** the universe via FMP: market cap > $1B, avg volume > 500K,
   price > $10, listed on NYSE/NASDAQ.
2. **Filters out** any ticker reporting earnings within the next 16 days (FMP
   earnings calendar), and prioritizes the most liquid names down to 40–60 tickers.
3. Always pulls **SPY** and **QQQ** for macro context, plus **VIX** from Yahoo Finance.
4. **Fetches per ticker:**
   - FMP: 250 daily bars (+ resampled weekly), 1-hour candles (10 days),
     and locally-computed RSI(14) daily & weekly, MACD(12/26/9) daily,
     ATR(14) daily, EMA 8/21/50/200 daily, 5d/20d return, nearest earnings date.
   - Unusual Whales: current IV, 52-week IV rank, put/call ratio, bullish
     unusual-flow alerts (premium > $50K, past 5 days), top-5 open-interest strikes.
5. **Aggregates** everything into one structured JSON payload (including 5d/20d
   relative strength vs SPY).
6. **Analyzes** with Claude across six confluence categories (trend, momentum,
   volatility, options market, smart-money flow, macro). A card is emitted only
   when **≥ 3 confluences** fire and confidence is **High** or **Medium**.
7. **Renders** one card per qualifying setup, sorted by confidence then confluence
   count, to `output/latest.html` (auto-refreshes every 5 min) and archives a
   timestamped copy under `output/history/`.

> **Note on technical indicators:** RSI/MACD/ATR/EMA are computed locally from FMP
> OHLCV (pandas/numpy) rather than FMP's indicator endpoints. This keeps MACD/ATR
> available on the Starter plan and guarantees consistent math across the universe.

---

## Project structure

```
flowscanner/
├── main.py                 # entry point, orchestrates a scan
├── config.py               # API keys, constants, all tunable scan parameters
├── scheduler.py            # built-in daemon + Task Scheduler notes
├── requirements.txt
├── .env.example            # copy to .env and fill in keys
├── data/
│   ├── fmp.py              # FMP: screener, OHLCV, indicators, earnings
│   ├── unusual_whales.py   # UW: IV rank, P/C, flow, OI
│   ├── vix.py              # VIX from Yahoo Finance
│   └── aggregator.py       # combines sources into one payload per ticker
├── analysis/
│   ├── prompt_builder.py   # formats payload into the Claude prompt
│   └── claude_engine.py    # calls Claude, parses trade cards
├── output/
│   ├── renderer.py         # generates the HTML output
│   ├── template.html       # base HTML template (Jinja2)
│   ├── latest.html         # current session (generated)
│   └── history/            # timestamped archives (generated)
└── logs/                   # rotating log files (generated)
```

---

## Setup (Windows)

Requires **Python 3.11+**.

```powershell
cd C:\Users\<you>\flowscanner

# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure API keys
copy .env.example .env
notepad .env      # paste your three keys
```

### API keys (where to get them)

| Key | Provider | Where |
| --- | --- | --- |
| `FMP_API_KEY` | Financial Modeling Prep (Starter plan) | https://site.financialmodelingprep.com/developer/docs |
| `UW_API_KEY` | Unusual Whales API | https://unusualwhales.com/public-api |
| `ANTHROPIC_API_KEY` | Anthropic | https://console.anthropic.com/ |

Keys live only in `.env`, which is gitignored and never committed.

---

## Running

```powershell
# One-shot scan immediately (for testing, or from the OS scheduler)
python main.py --run now

# Built-in scheduler daemon (fires all three sessions/day, stays running)
python main.py
```

Open the result in a browser:

```
output/latest.html
```

The page auto-refreshes every 5 minutes, so you can leave it open during a session.

---

## Configuring Windows Task Scheduler (recommended)

Instead of leaving the daemon running, schedule three tasks that each call
`--run now`. This is the most reliable option on Windows.

For **each** session time (premarket 08:00, midday 12:30, postmarket 16:30 —
edit as desired):

1. Open **Task Scheduler** → **Create Task…**
2. **General** tab: name it e.g. `FlowScanner Premarket`. Check
   *"Run whether user is logged on or not"*.
3. **Triggers** tab → **New…** → *Daily*, set the start time (e.g. `08:00`),
   recur every 1 day. (Optionally restrict to weekdays.)
4. **Actions** tab → **New…**:
   - **Program/script:** `C:\Users\<you>\flowscanner\.venv\Scripts\python.exe`
   - **Add arguments:** `main.py --run now`
   - **Start in:** `C:\Users\<you>\flowscanner`
5. **Conditions/Settings:** optionally *"Wake the computer to run this task"* and
   *"Run task as soon as possible after a scheduled start is missed"*.
6. Click **OK** and repeat for the midday and postmarket times.

Quick way to create one from PowerShell:

```powershell
$py   = "C:\Users\<you>\flowscanner\.venv\Scripts\python.exe"
$dir  = "C:\Users\<you>\flowscanner"
$act  = New-ScheduledTaskAction -Execute $py -Argument "main.py --run now" -WorkingDirectory $dir
$trg  = New-ScheduledTaskTrigger -Daily -At 8:00am
Register-ScheduledTask -TaskName "FlowScanner Premarket" -Action $act -Trigger $trg
```

Repeat with `-At 12:30pm` and `-At 4:30pm` for the other two sessions.

---

## Tuning

All scan parameters are in **`config.py`** and are safe to edit:

- `MIN_MARKET_CAP`, `MIN_AVG_VOLUME`, `MIN_PRICE`, `EXCHANGES` — universe filter
- `EARNINGS_EXCLUSION_DAYS` — skip near-term earnings reporters
- `TARGET_MIN_TICKERS` / `TARGET_MAX_TICKERS` — post-filter universe size
- `MIN_FLOW_PREMIUM`, `FLOW_LOOKBACK_DAYS`, `TOP_OI_STRIKES` — options/flow
- `MIN_CONFLUENCE_COUNT` — minimum confluences to emit a card
- `SCAN_SESSIONS` — the three daily run times
- `AUTO_REFRESH_SECONDS` — HTML refresh cadence
- `FMP_RATE_PER_MIN` / `UW_RATE_PER_MIN` / `HTTP_MAX_CONCURRENCY` — rate limits
- `CLAUDE_MODEL` — the analysis model

### Unusual Whales endpoints

UW response shapes and paths vary by plan. The endpoint templates
(`UW_OPTIONS_VOLUME`, `UW_OPTION_CONTRACTS`, `UW_FLOW_ALERTS`) live in `config.py`
and the parsers in `data/unusual_whales.py` are deliberately defensive — if a
field or path differs on your subscription, adjust those constants. Missing UW
fields degrade gracefully: the ticker is still analyzed with whatever data is
available.

---

## Error handling & logs

- A failed FMP/UW call for a single ticker is logged and skipped — the scan
  continues with the rest of the universe.
- If the Claude call fails, it retries once after 10 seconds before logging the
  failure and rendering an error banner.
- All errors and progress go to a **rotating** log file at `logs/flowscanner.log`
  (5 × 2 MB) plus the console.

---

## How a confluence card is scored

Claude evaluates six categories per ticker:

1. **Trend alignment** — daily/weekly EMA structure, price vs EMAs, market structure
2. **Momentum** — daily/weekly RSI, MACD
3. **Volatility setup** — IV rank, ATR-based range
4. **Options market** — IV-rank favorability, put/call skew, OI clustering
5. **Smart-money flow** — unusual bullish flow, premium size, directionality
6. **Macro alignment** — SPY trend, VIX regime, relative strength

A trade card requires **≥ 3** categories firing and **High/Medium** confidence.
Each card includes: ticker + calls bias, recommended contract (expiration, strike,
delta target), 2–3 sentence thesis, the confluence signals that fired, confidence
tier, structural stop, technical price target, estimated R/R, and an IV assessment.

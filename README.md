# FlowScanner

A stock-options scanner that runs three times each weekday (premarket 09:00,
confirmation 10:15, pulse 14:00 ET) to identify high-probability swing **call**
setups using multi-confluence analysis.

It screens a large-cap US-equity universe, pulls price/technical/options/flow data,
sends a single structured payload to **Claude (`claude-opus-4-8`)** for confluence
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

### GitHub Pages publishing config

Publishing the report needs three non-sensitive settings plus a token:

| Name | Sensitive? | Where it comes from |
| --- | --- | --- |
| `REPO_OWNER` | No | Actions **variable** (or `.env` locally) |
| `REPO_NAME` | No | Actions **variable** (or `.env` locally) |
| `PAGES_URL` | No | Actions **variable** (or `.env` locally) |
| `GH_TOKEN` | Yes | Provided automatically by Actions; `.env` only for local runs |

`REPO_OWNER`, `REPO_NAME`, and `PAGES_URL` carry no secret material — add them
under **Settings → Secrets and variables → Actions → Variables** tab, *not* as
secrets.

`GH_TOKEN` is never configured by hand in Actions: every workflow run already
receives `${{ secrets.GITHUB_TOKEN }}`, which the workflow maps to `GH_TOKEN`.
You only set `GH_TOKEN` yourself in your local `.env` (a personal access token)
when running the scanner on your own machine.

> These names deliberately avoid the `GITHUB_` prefix — GitHub Actions reserves
> it and refuses to create secrets or variables that start with it.

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

## Running on GitHub Actions (primary)

The scanner runs itself on GitHub's hosted runners — no machine of yours needs to
be on. The workflow lives at [`.github/workflows/scan.yml`](.github/workflows/scan.yml)
and fires three times each weekday:

| Session | ET (primary) | Purpose |
| --- | --- | --- |
| `premarket` | 9:00 AM | **Plan** — 30 min before the bell, off yesterday's completed bar. |
| `confirmation` | 10:15 AM | **Confirm / change-detection** — ~45 min after the open, once intraday volume and price action are meaningful; notes what changed vs the premarket read (volume confirming, gap held/faded, breakouts going "approaching" → "breaking"). |
| `pulse` | 2:00 PM | **Reassess** — re-reads today's live bar past the lunch chop, with time left to act before the close. |

Each session has a **primary** trigger (a Cloudflare Worker, on time — see
[`cloudflare/`](cloudflare/)) and a **backup** trigger (GitHub's own `schedule`
crons, offset ~15 min later so they no-op when the primary already ran). The
workflow resolves the session from the real Eastern clock and passes it to
`main.py` as the `SESSION_TYPE` env var.

> **EDT/EST drift caveat — applies to all three sessions.** Cron is fixed UTC and
> does not follow DST, so both schedulers register the EDT *and* EST firing of each
> session and gate on the real Eastern hour. The ET times above are the EDT
> (summer) targets; the crons are pinned to those UTC minutes, so an EST-only line
> lands an hour off until its twin takes over. Changing a session time means
> updating both `.github/workflows/scan.yml` and `cloudflare/wrangler.toml`.

### Secrets and variables to add

Under **Settings → Secrets and variables → Actions**:

**Secrets** tab — the three API keys:

- `FMP_API_KEY`
- `UW_API_KEY`
- `ANTHROPIC_API_KEY`

**Variables** tab — the non-sensitive publishing config (`REPO_OWNER`,
`REPO_NAME`, `PAGES_URL`). See [GitHub Pages publishing config](#github-pages-publishing-config)
above.

Do **not** create a `GH_TOKEN` secret. Every workflow run already receives
`${{ secrets.GITHUB_TOKEN }}` automatically; the workflow maps it to `GH_TOKEN`
for the Python step, and `permissions: contents: write` at the top of the
workflow lets that token push commits back to the repo.

### How history survives an ephemeral runner

Runners get a clean disk every run, so `output/history/` — which `main.py` reads
to compute day-over-day streaks — is persisted **in the repo itself**. The
workflow pulls before the scan so the folder is populated, then commits the new
timestamped history file plus `output/latest.html` back to `main` afterwards
("Automated scan: {session} {date}"). `main.py` still reads history from local
disk exactly as it did before; only the workflow changed. Because of this,
`output/history/` is deliberately **not** gitignored.

### Triggering a run by hand

**Actions** tab → **FlowScanner Scan** → **Run workflow**. The optional
`session_type` input (default `manual`) overrides the cron-derived session name
and becomes the prefix of the archived history file.

---

## Configuring Windows Task Scheduler (local alternative)

GitHub Actions is the primary schedule; the options below still work if you'd
rather run the scanner on your own machine.

Instead of leaving the daemon running, schedule two tasks that each call
`--run now`. This is the most reliable option on Windows.

For **each** session time (morning 09:00, afternoon 14:00 — edit as desired):

1. Open **Task Scheduler** → **Create Task…**
2. **General** tab: name it e.g. `FlowScanner Morning`. Check
   *"Run whether user is logged on or not"*.
3. **Triggers** tab → **New…** → *Daily*, set the start time (e.g. `09:00`),
   recur every 1 day. (Optionally restrict to weekdays.)
4. **Actions** tab → **New…**:
   - **Program/script:** `C:\Users\<you>\flowscanner\.venv\Scripts\python.exe`
   - **Add arguments:** `main.py --run now`
   - **Start in:** `C:\Users\<you>\flowscanner`
5. **Conditions/Settings:** optionally *"Wake the computer to run this task"* and
   *"Run task as soon as possible after a scheduled start is missed"*.
6. Click **OK** and repeat for the afternoon time.

Quick way to create one from PowerShell:

```powershell
$py   = "C:\Users\<you>\flowscanner\.venv\Scripts\python.exe"
$dir  = "C:\Users\<you>\flowscanner"
$act  = New-ScheduledTaskAction -Execute $py -Argument "main.py --run now" -WorkingDirectory $dir
$trg  = New-ScheduledTaskTrigger -Daily -At 9:00am
Register-ScheduledTask -TaskName "FlowScanner Morning" -Action $act -Trigger $trg
```

Repeat with `-At 2:00pm` for the afternoon session.

---

## Tuning

All scan parameters are in **`config.py`** and are safe to edit:

- `MIN_MARKET_CAP`, `MIN_AVG_VOLUME`, `MIN_PRICE`, `EXCHANGES` — universe filter
- `EARNINGS_EXCLUSION_DAYS` — skip near-term earnings reporters
- `TARGET_MIN_TICKERS` / `TARGET_MAX_TICKERS` — post-filter universe size
- `MIN_FLOW_PREMIUM`, `FLOW_LOOKBACK_DAYS`, `TOP_OI_STRIKES` — options/flow
- `MIN_CONFLUENCE_COUNT` — minimum confluences to emit a card
- `SCAN_SESSIONS` — the three daily run times (premarket / confirmation / pulse)
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

# N500 Stock Screener

A single-user, advisory-only stock screener for the Indian market (NSE). It scans
stock universes with several custom strategies, stores the shortlists, pushes
Telegram alerts and shows everything on a dark, terminal-style web dashboard.

**It never places trades.** You review every shortlisted stock on the FYERS
terminal and make the final decision yourself.

---

## Status

| Phase | Scope | State |
| ----- | ----- | ----- |
| 1 | Project structure, config, environment variables, README | done |
| 2 | Data layer: FYERS provider, global-index provider, caching, tests | done |
| 3 | Strategy framework: base class, registry, scheduler, SQLite retention | done |
| 4 | One strategy end to end (from its spec) with tests | done |
| 5 | Telegram alerts | done |
| 6 | API endpoints | done |
| 7 | Dashboard UI: Market Overview + strategy results | done |
| 8 | Remaining strategies, one at a time | in progress (unified EOD Compression Breakout v4 done; Guppy legacy/manual) |
| 9 | Hardening: error handling, logging, rate limits, final README | pending |

Strategy logic is **not** invented here. Each strategy is implemented from a
spec / transcript supplied separately; if a spec is unclear the build stops and
asks.

---

## Architecture

One always-on Python service contains the scheduler, strategy engine, SQLite
database, HTTP API and the dashboard. The UI talks only to the API, so a mobile
front end can be added later without touching screening logic (NFR-2).

```
n500-stock-screener/
├── app/
│   ├── main.py            # FastAPI app entry point (health + static dashboard)
│   ├── config.py          # typed settings loaded from environment / .env
│   ├── core/
│   │   ├── logging.py     # logging setup
│   │   └── market.py      # NSE market hours + trading-calendar helpers
│   ├── data/              # Phase 2: market-data providers behind one interface
│   ├── strategies/        # Phase 3: base class, registry, metadata
│   ├── storage/           # Phase 3: SQLite run history (rolling retention)
│   ├── scheduler/         # Phase 3: APScheduler jobs
│   ├── api/               # Phase 6: FastAPI routers
│   └── static/            # Dashboard shell (HTML/CSS/JS, no build step)
├── tests/
├── .env.example           # copy to .env and fill in
├── requirements.txt       # runtime dependencies
├── requirements-dev.txt   # test / lint dependencies
└── pyproject.toml         # pytest + ruff configuration
```

Technology: Python, pandas, a free TA library, FastAPI, APScheduler, SQLite, and
a hand-written HTML/CSS/JS dashboard served by FastAPI (no Node.js, no build
step). Telegram alerts via the Telegram Bot API.

### Data layer (Phase 2)

All market data flows through `app.data.MarketDataProvider` so a different
source can replace FYERS later without touching strategies.

* `app.data.fyers.FyersProvider` - NSE quotes and history via the official
  `fyers-apiv3` SDK. Quotes are batched up to 50 symbols per call; history is
  fetched per symbol.
* `app.data.fyers.FyersAuthenticator` - FYERS OAuth. When `FYERS_FY_ID`,
  `FYERS_PIN` and `FYERS_TOTP_SECRET` are set, `ensure_token()` logs in fully
  unattended (send OTP -> verify TOTP -> verify PIN -> auth code -> token) and
  stores the tokens in `data/fyers_tokens.json` (git-ignored). A token stored
  earlier the same day is reused, so a login happens at most once per process
  per day. Manual fallback: open the URL from `get_auth_url()` and call
  `exchange_code()`. A pre-generated token can also be supplied via
  `FYERS_ACCESS_TOKEN`.
  **Note (verified live 2026-10):** FYERS has disabled its refresh-token API
  ("to comply with SEBI regulations", error `-16`), so the automated PIN +
  TOTP login replaces the old refresh flow.
* `app.data.groww_provider.GrowwProvider` - Groww Trade API provider for
  off-hours scans. Set `GROWW_API_KEY` (Bearer token from your Groww Trade API
  dashboard). Native 10-minute bars are supported (max 30 days per request;
  wider ranges are chunked automatically), along with daily and hourly bars.
  Quotes are derived from the last two daily bars. Historical data is a paid
  Groww add-on (~₹499/month early bird); orders remain free.
  **Note:** Groww is migrating its historical-data endpoints; the endpoint path
  in `app/data/groww_provider.py` should be checked against the first live
  response and adjusted if Groww changes it.
* `app.data.routing.RoutingProvider` - session-aware source routing so broker
  quota is only spent when it is necessary. `MARKET_PROVIDER` (FYERS) serves
  requests while the NSE session is open; outside session hours
  `OFFHOURS_MARKET_PROVIDER` (yfinance) is used, with automatic fallback to it
  whenever a live call fails.
* `app.data.yfinance_provider.YFinanceProvider` - free, keyless Yahoo Finance
  source for after-hours scans and development. Daily bars are native; Yahoo
  has no 10-minute bars, so 5-minute bars are resampled onto the NSE session
  grid (09:15, 09:25, ...), which reproduces FYERS' bar anatomy including the
  flat 15:15 closing-auction bar that `closed_candles` strips. Quotes are
  derived from the last two daily bars. Intraday history is capped at Yahoo's
  60-day window; intraday fetches older than that are clamped.
* `app.data.global_index.GlobalIndexProvider` - daily closes for the Market
  Overview via `yfinance` (Yahoo). Index mapping is in `INDEX_TICKERS`
  (Nifty `^NSEI`, Nasdaq `^IXIC`, DOW `^DJI`, S&P `^GSPC`, NYSE `^NYA`,
  Shanghai `000001.SS`, Nikkei `^N225`, Shenzhen `399001.SZ`, KOSPI `^KS11`,
  Euronext `^N100`). A day with no session maps to `None` ("Closed").
* `app.data.rate_limit.RateLimiter` - sliding-window limiter.
* `app.data.cache.CandleCache` - on-disk candle cache with append/merge
  semantics (warm cache, refetch only the missing tail).

**Verified FYERS Standard-plan Data API limits** (support.fyers.in, updated
2026-10-05): 5 requests/second, 50 requests/minute, 5,000 requests/day;
quotes accept up to 50 symbols; intraday history covers the last year. Breaching
the per-minute cap three times in a day blocks the account until midnight. The
limiter therefore defaults to 4/s, 40/min, 4,000/day and the cache/minimise
pattern keeps total calls far below that.

`tests/` covers the limiter, cache, FYERS parsing/auth, the global-index
provider (yfinance mocked), the off-hours yfinance provider and the routing
logic (all providers faked/mocked - no network).

### Strategy framework (Phase 3)

* `app.strategies.base` - `Strategy` ABC, `StrategyMetadata`, `Category`
  (Intraday / Short-term / Long-term / Event-driven) and the `Signal` result
  type (symbol, time met, price + optional metadata).
* `app.strategies.registry` - discovers every strategy module that exposes a
  `strategy_class`. **Adding a strategy = dropping in a new module; no changes
  to engine, dashboard or alert code.**
* `app.strategies.universe` - symbol universes (`NIFTY_50`, `NIFTY_500`,
  `ALL_NSE`) loaded from `data/universes/*.json`, with a `download_nifty_500()`
  helper to refresh the Nifty 500 list from NSE archives.
* `app.storage.db.SignalStore` - SQLite persistence of runs with **rolling
  two-week retention** (FR-11) and `prune_old()` for automatic cleanup. One
  row per occurrence: re-evaluating the same candle (manual re-run, after
  hours, or the first run of the next session) stores nothing new, and
  Telegram is only alerted for newly stored signals.
* `app.scheduler.jobs.StrategyScheduler` - APScheduler 1-minute tick. Each tick
  works out which auto-strategies are due and starts each one in its own
  background thread, so a slow full-universe scan (e.g. the 10-minute Doji) can
  never block another strategy. A per-strategy running guard prevents
  overlapping runs, and `_last_run` is stamped on completion so a long scan
  re-arms its cadence instead of restarting immediately. Within one run the
  union of the strategies' universes is still fetched **once**
  (fetch-once-evaluate-many). Strategies can run on a fixed cadence
  (`frequency_minutes`) or once per trading day at a set time (`run_at`, with a
  30-minute catch-up window). Market-hours and per-strategy failures are handled
  so nothing crashes the service. Strategies may also define a `prefilter()` to
  prune their universe from batched quotes before any candle is fetched; if the
  prefilter fails the full universe is used as a fallback. Every run records
  live progress (stage, fetch counters, elapsed time) exposed at
  `GET /api/run/{name}/status` - the dashboard polls it and shows a pop-up
  with an animated bull-vs-bear arena, stage label, progress bar and timer.
  The bull charges when signals are found, the bear wins on failures.

Concrete strategies (Phase 4+) each live in their own module with a
`strategy_class` and are implemented from their spec - no logic is invented.

### First strategy: Multi-Timeframe Doji (Phase 4)

`app.strategies.doji.MultiTimeframeDojiStrategy` (universe **Nifty 500**,
auto, 10-minute tick). Implements the BRD spec exactly:

* **Doji geometry** on the last closed candle: `B/R <= 0.10` and
  `R >= 0.45 * ATR14` (Wilder's). Subtypes: dragonfly / gravestone /
  long-legged / standard. Only real continuous-session bars are evaluated:
  the in-progress 10-minute bar, today's partial daily candle and FYERS'
  closing-auction artifacts (the flat ~zero-volume 15:15 auction-window bar
  and the 5-minute 15:25 auction match - NSE continuous trading ends at
  15:15) are all ignored. Each signal is stamped with the matched candle's
  own timestamp (not the scan time), so the dashboard shows *when the candle
  printed*, not when the scan ran.
* **Four timeframes**: 10-min and daily are fetched once per symbol; weekly and
  monthly are built from daily in memory (no extra API calls). Week-end and
  month-end are detected from the NSE trading calendar.
* **Confluence score** (0-100): 25 pts per timeframe with a Doji, +10 for an
  RSI(14) extreme, +10 for volume >= 1.5x its 20-bar average. Stored as signal
  metadata - it ranks signals but does **not** gate alerts.
* Indicators (`app.strategies.indicators`) are pure pandas and unit-tested.

Open-question decisions taken (flagged for your review): weekly/monthly are
only evaluated on the last trading day of the week/month; bearish **gravestone**
Dojis are shown and labelled rather than excluded; the **subtype** and confluence
score are included as extra metadata beyond FR-9's three fields.

### Second strategy: EOD Compression Breakout (Phase 8, unified v4)

`app.strategies.compression.EodCompressionBreakoutStrategy` (universe **Nifty 500**,
auto, daily at **15:35 IST**, timeframes 10-min + daily context). This is the
single EOD strategy: the former Guppy module is now manual/legacy, so there is
one 15:35 alert stream instead of duplicate 15:20 and 15:35 runs.

The original all-or-nothing gate stack measured poorly (fired on none of 41
explosive days), so the strategy was rebuilt as a compressed-coil detector with
a quality score. v4 combines three indicators into one decision:

* **21-period Bollinger mid-line** (`BOLLINGER_PERIOD=21`, `STD=2.0`) is the
  reference/trigger line. A setup is only valid when price is at or above the
  mid (LONG) or at/below it (SHORT).
* **12-EMA Guppy ribbon** - fast `3,5,8,10,12,15`, slow `30,35,40,45,50,60`.
  Ribbon geometry (fast-above-slow, slow-ribbon slope, total spread) confirms
  trend alignment; a tight ribbon pinch contributes to compression quality.
* **RSI(9)** with EMA(3) vs SMA(21) confirmation. Cross/curl direction and the
  RSI level (not overbought/oversold for the direction) are scored.

**Dual EOD bar.** The 15:05 continuous bar (15:05-15:15) is the primary trigger
for flat coils, staircase coils and box ignitions. The 15:25 auction bar is
used only when it is fully closed (scan at 15:35) for midline reclaims,
staircase reclaims and short breakdowns where the 15:05 bar was still below the
mid. The flat 15:15 auction-window bar is always skipped.

**Five archetypes, all with explicit false-positive gates:**

* `STAIRCASE_COIL` (LONG) - morning impulse (>= ~1.2%) then a flat afternoon
  pipe resting on a rising slow ribbon.
* `FLAT_COIL` (LONG) - full-afternoon box (`box <= 1.0%`, `squeeze <= 0.50`,
  nozzle <= 0.35) above the 21-MA mid with a rising RSI/ribbon.
* `BOX_IGNITION` (LONG) - live close above a prior coiled box on >= 1.5x
  volume, with the prior box/nozzle compression gates.
* `MIDLINE_RECLAIM` (LONG) - prior bar below the 21-MA mid, current bar
  reclaiming it with a green close and a tight box.
* `SHORT_BREAKDOWN` (SHORT) - high-volume break below a distribution shelf,
  price below the mid, ribbon/RSI rolling over.

There is **no absolute-bandwidth free pass** (`ABS_SQUEEZE_MAX_BW` was deleted):
every signal must pass the relative squeeze/nozzle gates and reach the score
cutoff. The 1-hour path was removed because the dual-bar logic is 10-minute
specific.

**Scoring and emission.** The 0-100 score is compression-heavy:
setup/trigger quality (STAIRCASE 19, FLAT 15, IGNITION 15, MIDLINE_RECLAIM 20,
SHORT_BREAKDOWN 21), RSI/Guppy confirmation, participation (volume expansion,
PAR-20, wick asymmetry), and daily context (close vs EMA20/EMA50, 52-week-high
proximity, tight base, turnover). Only `band == "BUY"` rows at or above
`BAND_BUY_MIN=72` are emitted - HOLD/SKIP are never stored or shown - sorted by
score and capped at the top 15. If any SHORT candidate exists for a symbol, all
LONG candidates for that symbol are suppressed (one direction per symbol).
Signals are stamped with the **closed candle timestamp** (15:05 or 15:25), not
the scan time, and carry `direction`, `setup_type`, `trigger_bar`, `dual_bar`,
entry/limit/stop/targets and the full score breakdown.

**Two-stage scan (FR-13 style).** One batched quote pass keeps only symbols
whose day range is <= 3% (missing quotes are kept); only survivors get real
10-minute and daily candles (daily = 365 days, only for prefilter survivors).

### Guppy Ribbon Compression (v3.2, now manual/legacy)

`app.strategies.guppy_compression.GuppyCompressionStrategy` remains registered
for ad-hoc research but is **manual only** (`schedule_type="manual"`,
`run_at=None`). Its 12-EMA ribbon, box selection and LONG/SHORT classification
were folded into the unified `compression.py` above, so running it on a schedule
would duplicate the 15:35 alerts.

### Open-Equals-Low

`app.strategies.oel.OpenEqualsLowStrategy` (universe **Nifty 500**, auto every
10 minutes, 10-min + daily). The rule is literal: the **day's open price must
equal the day's low price** - the stock never traded below its open.

* **Live 10-minute variant**: uses the 09:15 candle's open and the minimum low
  across *all* completed 10-minute bars so far today. It only starts after one
  hour of trading (10:15 IST), because at 09:25 the first bar's low is
  trivially the open for almost every stock. It stops at the 15:15 continuous
  close so the EOD daily candle takes over.
* **Daily EOD variant**: the last completed daily candle must have open == low.
* **Quote prefilter**: before any candle history is fetched, one batched quote
  per symbol checks `open == low` (2-decimal match) on the live day. This is
  exactly the same gate as the 10-minute variant (the quote's day low is the
  minimum of today's bars) and, after the close, exactly the daily candle's
  gate. Only matches - plus symbols with a missing/incomplete quote - get
  10-minute and daily candles, which keeps a run to seconds instead of
  ~25 minutes for all 501 symbols. Matches are never filtered out; they are
  only ranked by the quality score below.
* Price and `move_pct` are the latest completed close and the move from the
  day's open (not stale first-candle values); the alert shows `open=... low=...`
  so the criterion is verifiable.
* **Quality ranking (not a filter)**: every open == low match is kept, then
  sorted by a 0-100 score so strong opening demand is at the top and thin
  matches are at the bottom. **65%** of the score is the 09:15 10-minute bar's
  volume versus the *same* bar over the previous 10 sessions (60% magnitude +
  40% percentile rank); 15% is the move from the open, 10% is the close's
  position in the day's range, and 10% is opening turnover (liquidity guard).
  Comparing the 09:15 bar with the previous *first* bars avoids the intraday
  volume seasonality that makes an opening bar look tiny next to a 14:00 bar.
  HIGH >= 70, MEDIUM >= 45, LOW < 45; the dashboard and Telegram show the
  score, quality label and `vol10=...x` (with the percentile), so low-volume
  matches visibly sort last. When 10-minute history is unavailable the daily
  volume ratio is used as a fallback. `passes_filters` remains informational.
* Weekly/monthly historical variants were removed because they are not
  day-level signals.

### Nifty 500 universe

`app.strategies.universe.download_nifty_500()` fetches the constituent list
from NSE archives into `data/universes/NIFTY_500.json` (already run: 501
symbols). Re-run it any time to refresh; the strategy reads the file at each
scan.

---

## Setup

Requires Python 3.11+ (developed on 3.14).

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements-dev.txt     # includes runtime deps

# 3. Create your environment file
cp .env.example .env                     # then edit .env

# 4. Run the service
python -m app.main                       # http://127.0.0.1:8000
```

Open <http://127.0.0.1:8000> for the dashboard, or
<http://127.0.0.1:8000/health> for a configuration snapshot.

### Tests and lint

```bash
pytest
ruff check .
```

### Production-style run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## Environment variables

All secrets live in `.env` (git-ignored) or the process environment - never in
code or the repository.

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `APP_NAME` | `N500 Stock Screener` | Display name |
| `ENV` | `development` | Enables reload when `development` |
| `LOG_LEVEL` | `INFO` | Root log level |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address |
| `TIMEZONE` | `Asia/Kolkata` | Market timezone |
| `DATABASE_PATH` | `data/screener.db` | SQLite file for run history |
| `RETENTION_DAYS` | `14` | Rolling history window (FR-11) |
| `FYERS_CLIENT_ID` | - | FYERS app id |
| `FYERS_SECRET_KEY` | - | FYERS app secret |
| `FYERS_REDIRECT_URI` | `http://127.0.0.1:8000/callback` | FYERS OAuth redirect |
| `FYERS_FY_ID` | - | FYERS login id (automated login) |
| `FYERS_PIN` | - | FYERS login PIN |
| `FYERS_TOTP_SECRET` | - | Base32 TOTP key (External 2FA) for automated login |
| `FYERS_ACCESS_TOKEN` | - | Optional pre-generated token |
| `TELEGRAM_BOT_TOKEN` | - | Telegram bot token |
| `TELEGRAM_CHAT_ID` | - | Target chat id |
| `TELEGRAM_ALERTS_ENABLED` | `true` | Master switch for alerts |
| `TELEGRAM_NOTIFY_EMPTY` | `false` | Send alerts for runs with no matches (FR-14) |
| `GLOBAL_INDEX_PROVIDER` | `yfinance` | Free global-index source (chosen in Phase 2) |

### FYERS authentication (background)

The FYERS access token expires daily. With `FYERS_FY_ID`, `FYERS_PIN` and
`FYERS_TOTP_SECRET` set (enable "External 2FA (TOTP)" in the FYERS account to
get the Base32 key), the data layer logs in unattended: send OTP, verify TOTP,
verify PIN, then exchange the auth code for tokens. Tokens are cached per day in
`data/fyers_tokens.json`. The refresh-token API is disabled by FYERS (SEBI,
error `-16`), so the login flow runs once per day instead. A pre-generated token
can still be pasted into `FYERS_ACCESS_TOKEN`. Verify the current free-plan data
limits in the FYERS API docs before relying on the figures quoted in the BRD.

---

## Design rules carried into later phases

- **Fetch once, evaluate many** - adding a strategy must not add API calls.
- **Two-stage intraday scan** - batched quotes pre-filter, then real 10-minute
  candles for candidates only (never rebuild candles from snapshots).
- **Weekly/monthly from daily** - resample in memory.
- **Rate limiter with safety margin** and backoff on rate-limit errors.
- **Accuracy first** - unit tests for indicator maths and each strategy's rules.
- **Resilience** - one failing strategy or API call never crashes the app.

## Out of scope (v1)

Order placement, trading bot, login / multi-user, parameter-editing UI,
backtesting engine, paid data feeds.

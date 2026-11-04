# N500 Stock Screener — Project Review Document

Prepared for independent AI review and future improvement assessment.

---

## 1. User's Original Vision & Strategy Logic (as stated across sessions)

### Core idea: Open-Equals-Low (OEL)
- **Criterion is literal and day-level**: the day's open price equals the day's low price. If the price never traded below the open, buyers controlled the session from the first trade.
- **Only gate is `open == low`**. No other filter excludes a signal. `passes_filters` (vol ≥ 1.5x, move ≥ 0.5%) remains informational, not a gate.

### Ranking / Sorting Requirement (explicit user instruction)
> "Give full list which are open=... low=... how Sort/Rank low quality stocks at the bottom. Good quality stock should be High Ranked based on volume."
> "Something like, the latest 10min candle which is also our first 10-minute candle meeting the OEL criteria should have greater volume than the previous 10 candles/previous few trading days"

Key user rules interpreted and implemented:
1. **Show the FULL list** (not truncated to 5). All 8 current daily signals are visible.
2. **Rank high-volume/high-quality at top**, low-quality at bottom.
3. **Compare 09:15 first 10-minute bar to the SAME bar over previous 10 sessions** — avoids time-of-day seasonality that would make an opening bar look weak vs a 14:00 bar.
4. **Volume column must be visible** so the signal day's volume is visibly compared with recent days.
5. **Keep `v3 app/strategies/compression.py` untouched**; v3.2 Guppy strategy stays separate (`guppy_compression.py`). *(Superseded 2026-10-31: the two were merged into one unified v4 `compression.py`; see section 8.)*
6. **No duplicate `app/core/fyers_auth.py`**; single token manager is `app/data/fyers.py::FyersAuthenticator`.
7. **Dashboard PIN protects public dashboard**; credentials only in `.env`.
8. **No service worker in the PWA**.
9. **OEL emits completed-candle signals on daily, weekly and monthly timeframes** (weekly/monthly restored 2026-11-04).
10. **Buy-band only for banded strategies**.
11. **Keep `app/strategies/oel.py` as the main engine** (weekly/monthly live in the same module).

---

## 2. Implementation Details & Design Decisions

### 2.1 OEL Engine (`app/strategies/oel.py`)

**Variants (weekly/monthly restored 2026-11-04):**
- **Intraday (10-minute)**: evaluated live during the session. Requires 6 completed bars (10:15 IST) to avoid noise at 09:25. Stops at 15:15 continuous close so daily candle takes over.
- **Daily (EOD)**: last completed daily candle where `open == low`.
- **Weekly**: last completed `W-FRI` candle resampled from daily where `open == low`.
- **Monthly**: last completed `ME` candle resampled from daily where `open == low`.
- The period advances on the first run of the new week/month (not at Friday/month-end close), so the prior period's daily bars have been refreshed with final values. Higher-timeframe signals are cached per completed-period label so the quote prefilter cannot hide them between runs.

**Critical fix applied (previous gap)**:
- Before: the code evaluated `most recent date` rather than `today`. At 09:30, this meant yesterday's data was evaluated, producing stale signals and blocking today's real signal.
- After fix: `today_mask = idx.date == now.date()` for the 10-minute variant. Daily uses `closed_candles` on `daily` frame.

**Volume-led quality score (0-100, ranking only, never filters):**
- **65% opening-volume score**:
  - 60% magnitude: log-scaled ratio (normalized at 3x → 100%)
  - 40% percentile: how many of the previous 10 sessions' 09:15 bars are beaten
- **15% move**: `(close - open) / open` scaled to 100% at 5% move
- **10% close location**: `(close - low) / (high - low)`
- **10% liquidity**: `turnover_cr` (open bar volume × close / 1e7)

**Labels**: HIGH ≥ 70, MEDIUM ≥ 45, LOW < 45, UNRATED when no volume history.

**Comparison logic** (`_first_bars` + `_volume_profile`):
- Extracts only the 09:15 10-minute bar for each session (not random intraday candles).
- Compares today's 09:15 volume to the previous `VOLUME_LOOKBACK = 10` sessions' 09:15 volumes.
- Returns `ratio`, `pctile`, `vs_prev`, `first_volume`, `turnover_cr`, `history_days`.
- If no 10-minute history exists, falls back to `daily_vol_ratio` (volume_ratio over 20 days of daily candles).

**State / Dedup**:
- Strategy scan is stateless (`_seen` removed). `save_run` handles dedup at storage layer.
- Storage refreshes price and metadata (`UPDATE runs SET run_at=?, price=?, metadata=?`) when the same `(symbol, signal_time)` is re-evaluated, so rankings stay current.

---

### 2.2 Storage (`app/storage/db.py`)

Key behaviors documented:
- Saves signals with `insert or ignore` based on `(symbol, timestamp)`.
- Returns only fresh signals (`fresh`) for alerting.
- Updates existing row's `run_at`, `price`, `metadata` when the same candle is re-scanned (so live price/rank updates persist without re-alerting).
- `get_latest` reads the most recent `run_at`.
- Pruning at `run_at < cutoff` (14-day retention).

---

### 2.3 Telegram (`app/alerts/telegram.py`)

Formatter updates:
- Sorts signals by `(score desc, open_vol_ratio desc, symbol)` for ranked strategies.
- Generic formatter adds rank (`#1`), `score=... QUALITY`, `vol10=...x (pct% of 10d)`, daily volume (`volD=...x`), and preserves `open=... low=...`.
- Uses `.env` token (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`); bot is `@N500_scan_bot`, chat ID `8507454356`.

---

### 2.4 Dashboard (`app/static/app.js` + `styles.css` + `index.html`)

UI updates implemented:
- `qualityPill()` shows HIGH/MEDIUM/LOW in colored badges.
- `scoreTooltip()` includes volume details (`vol10`, `pctile`, `daily_vol_ratio`, `first_bar_turnover_cr`, `score_parts`).
- `PREVIEW_ROWS` remains 5 for most strategies, but `Open-Equals-Low` uses `20` (shows all 8 current signals).
- **New Volume column (`volCell`)**: shows `3.99x` (primary, colored green/amber/red based on ratio), a `open` tag (for 09:15 bar) or `day` tag (for daily fallback), and a sub-line showing `day 2.2x` when both ratios exist. Tooltip shows full explanation of the comparison (prior 10 sessions, full-day ratio, turnover).
- CSS `.vol-cell`, `.vol-main`, `.vol-tag`, `.vol-sub`, `.strong`/`.ok`/`.weak` classes added.
- Cache bust: `styles.css?v=15`, `app.js?v=25`.

---

### 2.5 Tests Updated (`tests/test_oel.py`, `tests/test_storage.py`, `tests/test_telegram.py`)

- `test_intraday_ranks_high_opening_volume_first`: verifies HEAVY (3x) ranks above THIN (0.5x).
- `test_daily_quality_uses_first_10m_volume`: verifies daily EOD uses opening 10m volume.
- `test_daily_quality_falls_back_to_daily_volume_without_10m`: verifies daily-only data still scores.
- `test_scan_is_idempotent_for_10m` / `daily_eod`: verifies stateless scan produces the same candle (not empty, not duplicate key error).
- `test_save_run_refreshes_price_and_metadata_on_repeat`: verifies price updates from 100.0 → 105.0 and score from LOW (10) → HIGH (82.5) without creating a new alert.
- `test_oel_alert_ranks_by_quality_and_shows_volume`: verifies Telegram format includes `#1`, `HEAVY`, `score=88.0 HIGH`, `vol10=3.0x (100% of 10d)`, and `open=... low=...`.

---

### 2.6 Deployment & Operations

- Render free tier deployment (service + secrets configured).
- `.env` holds Telegram token/chat ID (`.env` not committed).
- No git repo existed before; `git init` needed.
- No service worker in PWA (confirmed no `service-worker.js`).
- `cron-job.org` ping to `/health` set for 09:00–15:45 IST.

---

## 3. Confirmed Results (Latest Verified Data)

### Background scan result (`/tmp/opencode/oel_ranked_run.json`, finished 2026-10-30 22:57)
**8 ranked daily OEL signals** (all `open == low`):

| # | Symbol | Score | Quality | vol10 (09:15 vs 10d) | Pctile | Daily vol (20d) | Open | Low | Move |
|---|--------|-------|---------|----------------------|--------|-----------------|------|-----|------|
| 1 | COFORGE | 94.2 | HIGH | 3.99x | 100% | 2.2x | 1972.0 | 1972.0 | 3.7% |
| 2 | UNITDSPR | 88.9 | HIGH | 2.84x | 90% | 1.61x | 1291.1 | 1291.1 | 3.3% |
| 3 | HOMEFIRST | 83.2 | HIGH | 6.17x | 100% | 3.22x | 1052.4 | 1052.4 | 1.86% |
| 4 | WIPRO | 51.5 | MEDIUM | 0.53x | 30% | 1.22x | 190.8 | 190.8 | 3.94% |
| 5 | PERSISTENT | 46.6 | MEDIUM | 0.45x | 30% | 1.4x | 5884.0 | 5884.0 | 2.79% |
| 6 | STLTECH | 36.9 | LOW | 0.8x | 40% | 0.48x | 824.5 | 824.5 | 0.0% |
| 7 | HCLTECH | 23.3 | LOW | 0.2x | 0% | 0.9x | 1410.0 | 1410.0 | 1.28% |
| 8 | DRREDDY | 22.6 | LOW | 0.18x | 10% | 0.73x | 1345.0 | 1345.0 | 0.52% |

### Database (`data/screener.db`)
- Strategy `Open-Equals-Low` has 736 total historical rows (includes older weekly/monthly rows from before the 2026-10-30 removal and the new 8 daily rows). The `variant` column now separates intraday/EOD/weekly/monthly occurrences on the same timestamp.
- Latest run (`run_at = 2026-10-30T22:57:52+05:30`) contains the 8 daily signals above with full metadata refreshed.

---

## 4. Known Gaps, Bugs, and Areas for Independent Review

### Bugs / Gaps identified by the user or discovered during work

1. **Previous gap: 10-minute variant evaluated yesterday's date** (`now` used `most recent date` instead of `today`). **Fixed**.
2. **Previous gap: `_seen` stateless tracking blocked re-scans** (`_seen` never cleared, so a second scan of the same valid candle returned nothing). **Fixed** (removed from strategy; storage handles dedup).
3. **Previous gap: weekly/monthly historical variants** emitted historical signals that were not true day-level OEL. They were removed on 2026-10-30 and **restored correctly on 2026-11-04**: only the latest *completed* weekly/monthly candle is emitted, built from daily resampling, with the in-progress period dropped. Old rows remain historical artifacts, not active signals.
4. **Previous gap: volume ranking did not compare to same 09:15 bar** (would have compared to random intraday candles). **Fixed** (`_first_bars` filters `time == 09:15`).
5. **Dashboard preview**: `PREVIEW_ROWS` was 5, which truncated the full 8-signal OEL list. **Fixed** (OEL uses 20 preview rows).
6. **No volume column on dashboard** before this session. **Fixed** (new `.vol-cell` column, tooltip, and CSS).
7. **Storage refresh**: previously a re-scan did not update live price or rank metadata. **Fixed** (`UPDATE` query in `save_run`).

### Potential remaining issues for independent review

8. **API endpoint `/api/run/Open-Equals-Low`** triggers a live scan that takes ~222 seconds after hours (yfinance). The `/health` endpoint may time out during this scan because the event loop is starved by the synchronous yfinance fetches. The scan output file (`/tmp/opencode/oel_ranked_run.json`) is written only after the full loop completes, which is correct, but long scans could cause server timeout warnings.
9. **No git repository exists** (`no` in environment). All work is uncommitted. The user explicitly wants this pushed to GitHub.
10. **PWA**: no service worker file (`service-worker.js` missing from workspace). Confirmed by search.
11. **`compression.py` (v3)** and `guppy_compression.py` (v3.2) remain untouched — verified by file modification times and content checks. The `guppy_compression.py` strategy is registered in `app/config.py` or registry but the file itself was not edited in this session.
12. **`.env` exposure risk**: `.env` contains `TELEGRAM_TOKEN` and chat ID. The `.gitignore` excludes `.env`, but since no `.git` exists, there is no protection. The user must initialize and add `.env` to `.gitignore` before pushing.
13. **OEL intraday variant stops at 15:15** (`CONTINUOUS_CLOSE`). Confirmed by `if now.time() >= CONTINUOUS_CLOSE: return None`. This is correct per user's instruction.
14. **Dashboard PIN**: no `PIN` variable is set in the current `.env` (not shown in conversation, but user states it exists and is protected). If the `.env` does not contain a `PIN` value, the PIN gate will not activate properly.
15. **Cache bust versions** (`styles.css?v=15`, `app.js?v=25`) are manually maintained. There is no automated build-time versioning.
16. **No mobile PWA install prompt** is visible in `index.html`. The user mentioned PWA on the checklist but did not provide manifest code. If a manifest/file exists, it should be verified.
17. **Full test suite**: 257 passed, 1 warning (`StarletteDeprecationWarning`). All `ruff` checks pass.
18. **Telegram alert** for the 8 ranked signals: the user mentioned a previous clean 8-signal Telegram alert was delivered successfully. After this ranked scan, the DB rows are updated with scores and the dashboard shows the ranked list. A manual one-off alert can be triggered with the new formatting if needed.

---

## 5. File Reference Map (for reviewer navigation)

| Component | File | Key Lines / Sections |
|-----------|------|----------------------|
| Strategy engine | `app/strategies/oel.py` | Lines 22-228 (full rewrite with `_first_bars`, `_volume_profile`, `_quality_metrics`, `_volume_metadata`, `scan`, `_scan_intraday`, `_scan_daily`) |
| Indicators | `app/strategies/indicators.py` | `_volume_ratio` (line 216); `resample` (line 197) |
| Storage / DB | `app/storage/db.py` | `save_run` (line 98, refresh logic line 168+), `get_latest`, `prune_runs` |
| Telegram | `app/alerts/telegram.py` | `_format` (ranking sort, line 81), `_signal_line_generic` (volume details, line 122), `send_strategy_results` |
| Dashboard JS | `app/static/app.js` | `volumeTooltip` (line ~317), `qualityPill` (line ~302), `volCell` (line ~433), `signalsTable` (line ~382) |
| Dashboard CSS | `app/static/styles.css` | `.quality-pill` (line 439), `.vol-cell` (line 470), `.vol-main`/`.vol-sub` |
| Dashboard HTML | `app/static/index.html` | Cache bust links (`v=25` for JS, `v=15` for CSS) |
| Tests (OEL) | `tests/test_oel.py` | `test_intraday_ranks...`, `test_daily_quality...`, `test_weekly_variant...`, `test_monthly_variant...`, `test_weekly_ignores_in_progress...`, `test_prefilter_returns_full_universe...` |
| Tests (Storage) | `tests/test_storage.py` | `test_save_run_refreshes...` (line 110) |
| Tests (Telegram) | `tests/test_telegram.py` | `test_oel_alert_ranks...` (line ~221) |
| README / docs | `README.md` | OEL section updated (line 243+) |
| Config / auth | `app/data/fyers.py` | `FyersAuthenticator` (single token manager) |
| Untouched v3 | `app/strategies/compression.py` | Confirmed untouched |
| Untouched v3.2 | `app/strategies/guppy_compression.py` | Confirmed untouched |
| Main app / routes | `app/` (other modules) | `scheduler/jobs.py`, `core/` etc.

---

## 6. Uncommitted Changes (current git status — no repo exists)

Since `git init` has not been executed, all files are untracked. The user explicitly asked to push to GitHub. To complete this, the user must either:
- Provide the GitHub repository URL and authentication method, OR
- Confirm `git init` + `git remote add origin <url>` + `git push -u origin main` is acceptable (requires `.env` excluded and `.gitignore` set).

**Note for reviewer**: Because `.env` contains secrets (`TELEGRAM_TOKEN`), the `.gitignore` must include `.env` before any push. The `.env` file is referenced in `app/config.py` and `app/alerts/telegram.py` but is not included in any committed file.

---

## 7. Final Confirmed State (as of 2026-10-30 23:00 IST)

- [x] OEL engine: `open == low` only gate; full 8-signal list ranked by volume-led quality score.
- [x] Ranking logic implemented (`_quality_metrics`, 65/15/10/10 weights).
- [x] 09:15 first-bar comparison (`_first_bars`, previous 10 same-bar sessions) implemented.
- [x] Volume column (`volCell`) visible in dashboard with tooltip showing `vol10`, `pctile`, `daily_vol_ratio`, `turnover_cr`.
- [x] CSS (`.vol-cell`, `.quality-pill`) implemented.
- [x] Dashboard cache bust (`v=25` JS, `v=15` CSS).
- [x] Weekly/monthly OEL variants restored correctly (latest completed candle only; in-progress period dropped).
- [x] Stateles scan (`_seen` removed); `save_run` refreshes price/metadata.
- [x] Telegram formatter updated (rank, score, quality, `vol10`, `open/low`).
- [x] `passes_filters` remains informational (not a gate).
- [x] `v3 compression.py` untouched.
- [x] No duplicate `fyers_auth.py` (`fyers.py::FyersAuthenticator` is single manager).
- [x] No service worker in PWA.
- [x] All 257 tests pass (1 warning); ruff clean.
- [x] Latest ranked scan (`/tmp/opencode/oel_ranked_run.json`) confirms 8 signals with full scores.
- [x] DB (`data/screener.db`) updated with 8 new daily signals (`run_at = 2026-10-30T22:57:52+05:30`).
- [x] `.env` exists (not committed); `.env` must be in `.gitignore` before push.
- [x] No `.git` repository yet; `git init` required before push.

---

## 8. Unified EOD Strategy Redesign (v4) — 2026-10-31

### 8.1 What changed

The separate v3 compression strategy and v3.2 Guppy strategy were merged into
one strategy in `app/strategies/compression.py`. `guppy_compression.py` is kept
as **manual/legacy** (`schedule_type="manual"`, `run_at=None`) for research only.

One indicator stack now drives every decision:

| Component | Setting |
|-----------|---------|
| Bollinger mid-line (reference/trigger) | period 21, 2.0 std |
| Guppy fast EMAs | 3, 5, 8, 10, 12, 15 |
| Guppy slow EMAs | 30, 35, 40, 45, 50, 60 |
| RSI | 9-period, EMA(3) vs SMA(21) confirmation |
| Box | last 15 x 10-min bars |
| Emission | 10-minute only (1-hour path removed) |
| Schedule | daily 15:35 IST (dual-bar: 15:05 + 15:25) |
| BUY cutoff | `BAND_BUY_MIN = 72.0` |
| Output | BUY-band only, score-sorted, top 15 |

**Dual-bar logic.** 15:05 is the trigger for `STAIRCASE_COIL`, `FLAT_COIL` and
`BOX_IGNITION`. 15:25 (only visible once closed at 15:35) is the trigger for
`MIDLINE_RECLAIM`, staircase reclaims, and `SHORT_BREAKDOWN` where the 15:05 bar
was still below the mid. The flat 15:15 auction-window bar is skipped.

**Deleted loopholes.** `ABS_SQUEEZE_MAX_BW` (absolute-bandwidth free pass) and
`hourly_from_10m` were removed. Every signal must pass relative squeeze/nozzle
gates and reach the BUY score cutoff. Signals are stamped with the closed candle
timestamp, not `datetime.now(IST)`.

**Direction handling.** `_scan` collects all matching archetypes, scores them,
keeps the best per symbol, suppresses all LONG candidates when a SHORT candidate
exists, and caps the result at 15. Storage gained a `direction` column with a
migration/backfill from metadata and a direction-aware unique index so LONG and
SHORT on the same candle remain distinct.

**Dashboard.** Latest and history views filter banded strategies to `band ==
"BUY"`; history shows explicit Direction and Setup columns with a direction
pill. This prevents stale HOLD/SKIP rows from an older version from appearing.

### 8.2 Gate tightening from replay

The first unified pass produced false positives. Fixes:

* Flat coil box tightened to `box <= 1.0%` and `squeeze <= 0.50`
  (rejects KOTAKBANK 30/10 and similar).
* Ignition nozzle tightened to `IGNITION_NOZZLE_MAX = 0.30`.
* 15:05 candidate priority is staircase -> flat coil -> box ignition; a stock
  already in a tight coil is reported as the coil, not a same-bar ignition
  (fixed MARICO 30/10 being mislabelled `BOX_IGNITION`).
* Weak ribbon tier = 3.0 points and weak RSI-cross tier = 2.0 points.
* Short daily context rewards breakdowns near the 52-week high
  (`_ramp(dist_52w, 0.20, 0.02)`) instead of already-extended stocks.

### 8.3 Replay validation (Fyers 10m + daily)

All cases below pass with `_scan(data, settings, 15:35)`:

| Symbol | Date | Result | Setup | Score | Trigger |
|--------|------|--------|-------|-------|---------|
| WIPRO | 2026-02-24 | LONG | MIDLINE_RECLAIM | 72.7 | 15:25 |
| HCLTECH | 2026-07-02 | LONG | STAIRCASE_COIL | 73.0 | 15:25 |
| TCS | 2026-07-27 | LONG | STAIRCASE_COIL | 75.1 | 15:25 |
| TCS | 2026-07-28 | rejected (gap follow-through, not required) | - | - | - |
| MARICO | 2026-10-30 | LONG | FLAT_COIL | 81.0 | 15:05 |
| SBIN | 2026-10-16 | LONG | FLAT_COIL | 81.8 | 15:05 |
| ZYDUSLIFE | 2026-09-25 | LONG | STAIRCASE_COIL | 80.7 | 15:05 |
| ZYDUSLIFE | 2026-09-29 | SHORT | SHORT_BREAKDOWN | 74.5 | 15:25 |
| IPCALAB, CLEANMAX, HBLENGINE, JSWCEMENT, ICICIGI, SHYAMMETL, KOTAKBANK | 2026-10-30 | rejected | - | - | - |

Note: an earlier ZYDUSLIFE 29/09 SHORT score of 77.2 came from a
`score_probe.py` bug (`rets >= -0.022` for SHORT); the corrected strategy score
is 74.5.

### 8.4 Tests and files

* `tests/test_compression.py` rewritten (22 tests): BUY-only end-to-end, candle
  timestamps, dual-bar behavior, top-15 cap, score cutoff, archetype gates,
  daily context, prefilter, and absence of the removed loopholes.
* `tests/test_storage.py`: LONG/SHORT same-candle distinction and legacy DB
  migration.
* `tests/test_guppy_compression.py`: Guppy is manual/legacy.
* `tests/test_api.py`: expects `run_at == "15:35"`.
* Full suite: **266 passed, 1 warning**; `ruff check app tests` clean.
* `app/static/app.js`: BUY-band filtering and history Direction/Setup columns.
* `app/scheduler/jobs.py`: docstring updated to 15:35.

---

## 9. 2026-11-02: OEL scan duration and scheduler blocking

**Symptom**: an Open-Equals-Low manual run was still fetching at 385/501
symbols after 23 minutes; a 10-minute Multi-Timeframe Doji auto run was also
in progress. Both fetched 10-minute + daily candles for the full Nifty 500.

**Root causes**
1. OEL had no quote prefilter, so every run cost ~1,000 Fyers history requests.
   At the internal safety limit of 40 requests/minute that is ~25 minutes.
2. `StrategyScheduler.tick()` called `_run_auto_due([name])` sequentially, so
   the slow Doji run held the tick and kept OEL idle until it finished.

**Fixes**
* `app/strategies/oel.py`: `prefilter()` keeps symbols whose batched quote has
  `open == low` at 2-decimal precision, plus symbols with a missing/incomplete
  quote. This is the exact live 10-minute gate (the quote's day low is the
  minimum of today's bars) and, after the close, the exact daily-candle gate.
  Matches are never filtered from the result; ranking is unchanged.
* `app/scheduler/jobs.py`: `tick()` dispatches each due strategy to its own
  daemon thread. `_claim`/`_release` prevent overlapping runs of the same
  strategy, `_last_run` is stamped on completion, and a manual trigger while
  the same strategy is running returns 0 instead of starting a duplicate.
* `app/data/cache.py`: cache writes are serialised and atomic
  (temp file + `os.replace`), so concurrent runs cannot read a half-written
  JSON cache file.

**Verified** (server restarted, market open)
* Manual OEL: **6.4 s**, prefilter kept 75/501 symbols, no errors.
* OEL triggered while a manual Doji run was fetching: OEL finished in
  **82.7 s** while Doji was still scanning, proving the tick no longer blocks.
* Full suite: **271 passed, 1 warning**; `ruff check app tests` clean.

Note: during market hours the daily variant's "last completed candle" is the
previous session. The quote prefilter is based on today's live day, so stale
previous-session daily signals are not re-emitted by the 10-minute cadence.
After the close (manual EOD run) the quote is the completed daily bar, so the
daily list is reproduced exactly.

## Nifty Hedge 23: expiry-day symbol and logic fixes (2026-11-03)

**Symptom**: the strategy could not resolve FYERS option symbols and several
position-management paths were wrong; the dashboard had no tentative strike.

**Fixes**
* `app/data/options.py`: correct FYERS weekly/monthly symbol formats,
  holiday-shifted expiries (9 Nov / 23 Nov 2026), `is_nifty_expiry_day`,
  `expiry_kind`, and safe same-date symbol candidates. Added
  `app/core/nse_holidays.json` so `is_trading_day` skips NSE holidays.
* `app/strategies/nifty_hedge_23.py`: fixed the market-close `_emit_exit`
  argument error, enabled averaging adds (the old code compared float levels
  to strings and returned early while a position was open), removed the
  double-counted premium (`combined` already includes both legs) and the forced
  minimum 2 lots beyond the capital cap, reset position state on re-entry, made
  profit-taking a partial BOOK instead of a full exit, initialised the mode
  from settings on the first scan, and added a thread-safe live
  `snapshot()`/`refresh_snapshot()`.
* `app/api/routes.py`: `GET /api/hedge23/state` (advisory-only snapshot,
  throttled to one quote refresh per minute).
* Dashboard: dedicated Hedge 23 panel with the tentative ATM strike, CE/PE
  FYERS symbols, premiums, combined premium, entry zone, recommended lots/cost,
  SL and position state; the strategy card hides previous-expiry signals.

**Verified** (live, 3 Nov 2026 09:16 IST)
* `/api/hedge23/state` resolved `NSE:NIFTY26N0323550CE/PE` (both present in the
  FYERS NSE_FO master), spot 23,538.85, ATM 23,550, combined 132.30.
* Manual run: 1.9 s, 0 new signals (combined far above the 17-28 zone).
* Full suite: **295 passed**; `ruff check app tests` clean.

## Nifty Hedge 23: trending straddle logic, radius strikes and targets (2026-11-03)

**Change**: replaced the plain ATM straddle entry with the supplied
NK Stock Talk "Nifty Hedge 23" trending straddle/strangle logic. Still
advisory only - the screener never places orders.

**New logic**
* **Nifty movement gate** (`classify_nifty_movement`): classifies the expiry
  session from Nifty 50 10-minute candles as `TRENDING_BULLISH`,
  `TRENDING_BEARISH`, `COILED_FOR_SURGE` or `RANGE_BOUND_CHOP` using the day
  range (>= 60 pts), directional efficiency (>= 0.25) and the EMA9/EMA21
  spread (>= 10 pts); a compression coil after a morning move is allowed.
  Entry is skipped while Nifty is range-bound chop. Before enough 10-minute
  bars exist, the spot quote OHLC is used as a fallback; with no OHLC at all
  the gate allows the trade (`TRENDING_READY`).
* **Radius strike selection**: one batched quote request evaluates the
  50-point radius (spot +50 CE / spot -50 PE), the 100-point radius and ATM,
  preferring 50 -> 100 -> ATM. A pair is eligible only when CE/PE premiums are
  balanced (within 15 pts or <= 2.2x) and the combined premium sits inside an
  active entry zone; otherwise it falls back to ATM.
* **Entry zones**: classic 17-28 (SL 10) and extended 35-55 (ideal 40-50,
  SL 18); averaging mode still ladders 50/30/20.
* **Targets and time stop**: `target_1` = 2x combined premium (book half),
  `target_2` = 2.5x (or 110+ on the extended zone) to close the runner, and a
  **14:35 IST time-based stop** when Nifty turns range-bound and the combined
  premium has decayed >= 20% from the average entry. Hard exit stays 15:30.
* **Snapshot/Signals**: both now expose `movement`/`nifty_behaviour`,
  `ce_strike`/`pe_strike`/`radius_pts`, `legs_balanced`, `sl_price`,
  `target_1` and `target_2`. The dashboard mirrors the movement gate with a
  `range_bound_wait` status so it never advertises an entry during dead chop.

**Integration notes**
* `metadata.timeframes = ["10m"]` and `get_universe()` returns
  `NSE:NIFTY50-INDEX`, so the scheduler shares the Nifty 10-minute candles
  already fetched for the run. Verified FYERS returns 152 rows for that symbol.
* Snapshot `refresh_snapshot()` still uses quote OHLC (it does not run inside
  the scheduler), so the dashboard shows a quote-based classification; the
  authoritative 10-minute classification is computed during `scan()`.
* Once a position is open, `_fetch_leg_data()` fetches the **entered** CE/PE
  symbols instead of re-selecting a radius, so the dashboard, stop-loss and
  target checks never drift to strikes that were not bought.
* Tests expanded to 25 Hedge 23 cases: synthetic trending/chop/coiled 10-minute
  frames, extended-zone entry, 50/100 radius selection, leg-balance rejection,
  movement gate, time SL, target-2 exit, snapshot fields and open-position
  strike stability when spot moves.

**Verified**
* Full suite: **308 passed, 1 warning**; `ruff check app tests` clean.
* Dashboard renders the new movement/radius/targets panel with no console
  errors; after-hours state correctly reports `market_closed`.
* Live intraday validation of the movement gate was not possible at 17:57 IST
  (market closed); the next expiry is Monday 9 Nov 2026 (10 Nov holiday-shifted).

---

## 10. OEL Weekly/Monthly Restoration — 2026-11-04

### What changed
* **Weekly variant**: resamples the closed daily frame to `W-FRI` and evaluates
  the last completed week before the current one. Emits `variant="weekly"`,
  `timeframe="weekly"` and `week_open/low/high/close` metadata.
* **Monthly variant**: resamples to `ME` and evaluates the last completed month
  before the current one. Emits `variant="monthly"`, `timeframe="monthly"` and
  `month_open/low/high/close`.
* The period advances on the **first run of the new week/month**, not at
  Friday's or month-end's close. The daily cache can still hold a partial
  current-day bar fetched earlier that afternoon; waiting for the next
  period's first run guarantees the prior period's daily bars were refreshed
  with final values.
* Higher-timeframe signals are cached per completed-period label and re-emitted
  on every scan, so the day-level quote prefilter cannot hide them between
  runs. On a period-key change the prefilter returns the full universe once so
  daily candles exist for every symbol; normal runs use the cheap quote gate.
* Weekly/monthly ranking uses the completed period's volume versus its own
  history (20 weeks / 12 months); the 09:15 opening-bar score remains the
  intraday/EOD basis.
* SQLite `runs` now has a `variant` column, included in the unique dedupe index,
  so weekly and monthly signals that share a period-end timestamp cannot
  collide. Older databases are migrated and backfilled from metadata.
* Dashboard adds **Weekly** and **Monthly** OEL subtabs; the Open/Low cell reads
  `day_*`, `week_*` or `month_*` and the volume column shows the period ratio.
* Fixed an off-by-one in `is_last_trading_day_of_week`: Friday was incorrectly
  treated as a mid-week day because the loop included the following Monday.
  This also fixes weekly Doji evaluation on Fridays.

### Verified
* Full suite: **317 passed, 1 warning**; `ruff check app tests` clean.
* Real cached daily data (4 Nov 2026) produced 14 completed weekly and 3
  completed monthly OEL matches; spot checks (ACUTAAS, MPHASIS, TARIL,
  LEMONTREE, PERSISTENT) carried correct `open == low` and period labels.
* The new variants add no FYERS timeframes and no extra API calls beyond the
  once-per-period full-universe daily refresh.

---

## 11. Multi-Timeframe Doji Paused on Prod — 2026-11-04

* **Changed** `MultiTimeframeDojiStrategy.metadata` from `schedule_type="auto"`
  (10-minute tick) to `schedule_type="manual"`. The scheduler now skips it; the
  dashboard still shows the card and its **Run now** button for ad-hoc runs.
* **Why**: the strategy evaluates 10-minute and daily candles across all 501
  Nifty 500 symbols. A cold full-universe fetch is ~1,000 FYERS history calls
  and takes ~25 minutes at the 40/min internal limit, so it cannot sustain a
  10-minute cadence. It was also the main consumer of the daily API budget and
  was running on stale intraday cache data after the first scan of the day.
* **Not removed**: the module, tests, indicators and dashboard rendering remain
  intact. Re-enable by restoring `schedule_type="auto"` and
  `frequency_minutes=10` once the intraday data path (prefilter/WebSocket) can
  support it.
* **Note**: the strategy computes its week-end/month-end flags in `__init__`
  from the process start date. If it is run manually days after startup, those
  flags can be stale; fix before relying on its weekly/monthly evaluation.

---

*This document is intended to provide an independent AI reviewer with complete context, design rationale, confirmed results, and identified gaps so that further improvements or mistakes can be identified without ambiguity.*

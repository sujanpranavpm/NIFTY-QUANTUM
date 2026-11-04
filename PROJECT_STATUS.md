# PROJECT_STATUS.md

Last updated: 2026-11-04 (P0 repository audit + P1 data feasibility, complete)
Verified by direct inspection of the working tree and of the real data files — not from
prior progress reports or provider marketing pages.

## What this project is

A personal quantitative research laboratory. Current implemented scope is **daily
NSE cash-equity backtesting of order-based strategies**. The stated end goal is
**NIFTY index options (CE/PE, weekly + monthly, 1m/5m/15m)**, which is **not yet
implemented**. P1 has now established what the zero-cost data can and cannot support:
see `DATA_FEASIBILITY_REPORT.md` for the verdict and the field-by-field classification.

## Environment (verified)

| Item | Value |
|---|---|
| Python | 3.12.10 (`C:\Users\Sujan\AppData\Local\Programs\Python\Python312`) |
| pytest | 9.1.1 |
| Git | 2.53.0.windows.1 — initialised in P0 |
| OS | Windows, PowerShell (use `;`, not `&&`) |
| Package root | `d:\Downloads\nifty_quantum` |
| Import path | No installed editable package; scripts need `$env:PYTHONPATH='d:\Downloads\nifty_quantum'` |
| Console encoding | Set `$env:PYTHONIOENCODING='utf-8'` — strategy notes contain `₹`, which crashes cp1252 stdout |
| Installed versions (measured) | pandas **3.0.1**, numpy 2.4.3, pyarrow 25.0.1, curl_cffi 0.17.0, pypdf 6.21.4, yfinance 1.7.0 |
| Not installed | `polars`, `duckdb`, `fastparquet` — any P2 code must use pandas + pyarrow |
| Requirement drift | `requirements.txt` pins `pandas>=2.2,<3.0` but 3.0.1 is installed and the suite passes on it. Recorded, not silently re-pinned |

## Implemented and verified

### T1–T7 — research platform (complete)
- `nifty_quant/config.py` — `ResearchConfig`: engine, costs, slippage, validation blocks; `intervals = ["1d"]` only.
- `nifty_quant/data/` — provider abstraction (`yfinance`, `csv`), parquet+json cache, NIFTY50 universe snapshot, calendar, OHLCV schema, quality gates.
- `nifty_quant/engine/` — `orders.py` (order model), `fill_engine.py` (next-bar fills, OCO/bracket decomposition, trailing, stop-limit), `accounting.py`, `costs.py` (real Indian delivery-cost model), `features.py`, `panel.py`, `backtest.py` (`run_backtest`, `StrategyRun`, `RunSlice`).
- `nifty_quant/strategies/` — 20 registered strategies `orders_01`…`orders_20`, registry enforces exactly 20 in order.
  - 18 flagged `applicable_to_nse_equity = True`.
  - 2 excluded: `orders_19` (FX lots), `orders_20` (futures Donchian) — documented, not adapted.
- `nifty_quant/validation/` — splits (dev/OOS with purge), walk-forward, sensitivity, regime, consistency, FDR statistics, `ValidationReport` + `save_validation`/`load_validation`.
- `nifty_quant/analytics/` — metrics and trade extraction.
- `nifty_quant/reporting/` — CSV export bundles (`export_all`), 14 Plotly figure builders, HTML report writer.
- `nifty_quant/research/` — `run_research`, `save_results`, `load_results`, `ResearchResults`.

### T8 — verdict-gated signal engine (complete, independently tested)
- `nifty_quant/signals/` — `schemas.py`, `gating.py`, `parsing.py`, `engine.py`, `io.py`, `README.md`.
- Signals: `BUY`, `SELL`, `HOLD`, `EXIT`, `FLAT`, `BLOCKED`.
- Gate: verdict must be `supported` **and** `enough_trades` **and** `oos_profitable` pass **and** strict-split restart must agree; any critical-check failure is an automatic denial. **No manual bypass exists.**
- `RunSlice` extended with `positions` + `pending` captured at the last bar.
- `export_all(..., signals=...)` writes root and per-strategy `signals.csv`.
- Tests: `tests/test_signals.py` — 11 tests.

## Test status (recorded 2026-11-04)

```
10 passed, 1 skipped in 18.83s   (P0 run 1 — `logs/pytest_baseline.txt`)
10 passed, 1 skipped in 14.02s   (P0 run 2 — reproducibility confirmed)
10 passed, 1 skipped in 11.21s   (P1 close-out re-run — tree still green)
```

Invoke it as `python -m pytest tests`. A **bare `pytest` from the repo root is not a valid
command here**: with no `pytest.ini`/`pyproject.toml`, it also collects
`vendor/quantjourney-bt/tests/` and aborts with 8 `ModuleNotFoundError: No module named
'backtester'` collection errors, which looks like a broken repo but is not.

Skip reason: `test_bracket_stop_target_and_risk_reward` — `orders_12` produced no
fresh BUY at the end of the 3-symbol smoke sample (it produced an EXIT on TCS), so
the bracket R/R assertion has nothing to check. Not a failure.

**Coverage gap (real):** `tests/` contains **only** signal-engine tests. There are
**no** tests for the cost model, fill engine, accounting, indicators, look-ahead
prevention at engine level, validation statistics, or reporting. This is planned
task T9 and it is **outstanding**.

## Git status

- Initialised in P0. Commits: `b0affd5` (T1–T8 baseline), `2f39c23` (P0 audit + docs),
  then the P1 close-out commit — read `git log --oneline` for the current head.
- Tracked files: 114 at the time of writing, before the P1 artifacts are added.
- Ignored: `data/cache/`, `data/raw/`, all `*.parquet`, `logs/`, `reports/`, `vendor/`, and
  every regenerable export bundle (`artifacts/report_smoke/`, `artifacts/signals_smoke/`,
  `artifacts/roundtrip/`). Verified in P1 with `git check-ignore` — no downloaded provider
  data and none of the 64 P1 logs are staged.
- Versioned deliberately: `artifacts/smoke/*.csv` + `artifacts/validation_smoke/*.csv`
  so the T8 suite runs from a fresh clone without re-fetching market data.
- No secrets or `.env` files exist in the tree; secret patterns are pre-emptively ignored.

## Actual research state — the important part

The only completed research run is a **smoke run**, and its scope is tiny:

| Dimension | Actual value |
|---|---|
| Interval | `1d` only |
| Symbols loaded successfully | **3** (RELIANCE, TCS, M_M) |
| Symbols in universe metadata | 50 (NIFTY50 snapshot) — not fetched |
| Strategies run | 6 (`orders_01`, `05`, `09`, `12`, `17`, `18`) |
| Strategies validated | 6 |
| Sessions | 4154, 2010-01-04 → 2026-10-30 |
| **Strategies passing validation** | **0** |
| Verdict distribution | 6 × `unsupported` |

Cached data on disk is 6 symbols, of which HDFCBANK and INFY were **rejected by the
quality gate** (NaN rows in ~21-row frames).

**Why nothing passes** is a scope problem, not yet a proven strategy problem: with
3 symbols, cross-sectional breadth, concentration, walk-forward and FDR significance
are almost structurally impossible to satisfy. This must be re-tested on a real
full-universe run before any strategy is called weak.

## P1 outcome — zero-cost NIFTY options data (measured, 2026-11-04)

Deliverables: `DATA_FEASIBILITY_REPORT.md` (sections A–F, every number traceable to a
named script under `scripts/_p1_*.py` and a log under `logs/`), `DATA_SOURCE_REGISTER.md`
(what each source is permitted to be used for), 37 audit scripts, 64 P1 log artifacts.

Findings that bind all later work:

1. **Prices are usable; open interest is not one continuous series.** The Hugging Face
   1-minute archive covers NIFTY options `2021-05-07 → 2026-07-02` (267 expiry files) and
   its daily close equals the exchange's `LastPric` on 100% of contracts from 2025-01-30.
   Contract identity matched the official bhavcopy on **15 of 15** audited days with
   `hf_only_contracts = 0`.
2. **Two independent defects both start on exactly 2025-01-01** (so they are a pipeline
   change, not market behaviour): OI becomes 1/100 of the exchange's units, and the
   intraday high/low begins to under-cover the exchange's reported daily range
   (15.7–44.7% of contracts below the official high, vs ≤1.31% in 2024).
3. **Verification floor is 2024-01-02** — that is the earliest date the free official NSE
   archive serves. `2021-05 → 2023-12` is single-source and unverifiable at ₹0.
4. **Depth, bid/ask spread and per-minute trade counts exist in no permitted source.**
5. Both optional providers are blocked: Cloud Trader Pro serves a Google sign-in wall
   (zero bytes obtained), ICICI Breeze requires a broker account. Neither is claimed.

## Pending tasks

| ID | Task | State |
|---|---|---|
| P0 | Repo audit, git, docs, test record | **Done** |
| P1 | Zero-cost NIFTY options data verification + feasibility report | **Done** — awaiting review |
| T9 | Real pytest suite: costs, fills, accounting, look-ahead, validation | Outstanding |
| T10 | Full-universe data fetch + 20-strategy research & validation | Outstanding |
| T11 | Strategy applicability screening → shortlist ~5 → keep ≤3 | Outstanding |
| — | FastAPI + DB layer + Streamlit dashboard (old plan t8 remainder) | Not started |
| — | Options data ingestion, options backtest engine, 1m/5m/15m support | Not started; P1 says **price** research is feasible, OI/depth are not |

## Known limitations (honest list)

1. **No options capability at all.** No contract model, no lot size, no expiry, no
   OI, no greeks, no options P&L. The current engine cannot trade CE/PE.
2. **Daily only in practice.** `ResearchConfig.intervals = ["1d"]`. `orders_15` (5m)
   and `orders_16` (30m) are registered but there is no cached intraday data.
3. **Survivorship bias is exposed, not corrected.** The NIFTY50 constituents file is
   a *current* snapshot applied across 2010–2026 history. `run_metadata` records
   `survivorship_bias_exposed`.
4. **Zero validated strategies** — the research objective is currently unmet.
5. **Test coverage is thin** — engine and cost code is untested by pytest.
6. Free-tier intraday history from yfinance is roughly 30–60 days for 5m bars and
   ~7 days for 1m bars (the provider's documented limit — **not re-measured in P1**, and
   NSE index-option chains were never queried through it); that alone cannot support
   2021-onwards 1-minute research. P1 confirmed no other permitted provider closes this —
   the Hugging Face archive is the only one with multi-year 1m option history.
7. **No 15-minute interval exists at any permitted provider** (Breeze documents
   `1minute/5minute/30minute/1day` only). 15m must be resampled from 1m.
8. **Option cost model is missing.** `nifty_quant/engine/costs.py` is an equity
   delivery-cost model; per-lot derivative charges, stamp duty and exchange transaction
   fees for options are not modelled, so no options P&L number is trustworthy yet.
9. **Terminal-bar convention unresolved (report W3).** Audited files carry 376 minute
   stamps `09:15 … 15:30` with both endpoints present, while `2022-09-22` ends at 15:29
   (375 stamps) and 9 of 42 days in `2023-12-28` differ again — so a 1-minute bar cannot be
   assumed to be labelled at its start without deciding and recording the convention first.

# PROJECT_STATUS.md

Last updated: 2026-11-04 (P0 repository audit)
Verified by direct inspection of the working tree — not from prior progress reports.

## What this project is

A personal quantitative research laboratory. Current implemented scope is **daily
NSE cash-equity backtesting of order-based strategies**. The stated end goal is
**NIFTY index options (CE/PE, weekly + monthly, 1m/5m/15m)**, which is **not yet
implemented** and is gated on the P1 data-feasibility findings.

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
10 passed, 1 skipped in 18.83s   (run 1)
10 passed, 1 skipped in 14.02s   (run 2 — reproducibility confirmed)
```

Skip reason: `test_bracket_stop_target_and_risk_reward` — `orders_12` produced no
fresh BUY at the end of the 3-symbol smoke sample (it produced an EXIT on TCS), so
the bracket R/R assertion has nothing to check. Not a failure.

**Coverage gap (real):** `tests/` contains **only** signal-engine tests. There are
**no** tests for the cost model, fill engine, accounting, indicators, look-ahead
prevention at engine level, validation statistics, or reporting. This is planned
task T9 and it is **outstanding**.

## Git status

- Initialised in P0; single baseline commit `b0affd5` on `master`.
- 108 tracked files, ~644 KB.
- Ignored: `data/cache/`, all `*.parquet`, `logs/`, `reports/`, `vendor/`, and every
  regenerable export bundle (`artifacts/report_smoke/`, `artifacts/signals_smoke/`,
  `artifacts/roundtrip/`).
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

## Pending tasks

| ID | Task | State |
|---|---|---|
| P0 | Repo audit, git, docs, test record | **Done** |
| P1 | Zero-cost NIFTY options data verification + feasibility report | In progress |
| T9 | Real pytest suite: costs, fills, accounting, look-ahead, validation | Outstanding |
| T10 | Full-universe data fetch + 20-strategy research & validation | Outstanding |
| T11 | Strategy applicability screening → shortlist ~5 → keep ≤3 | Outstanding |
| — | FastAPI + DB layer + Streamlit dashboard (old plan t8 remainder) | Not started |
| — | Options data ingestion, options backtest engine, 1m/5m/15m support | Not started, blocked on P1 |

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
   ~7 days for 1m bars; that alone cannot support 2021-onwards 1-minute research.

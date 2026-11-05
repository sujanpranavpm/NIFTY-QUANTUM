# PROJECT_STATUS.md

Last updated: 2026-11-05 (P0 + P1 + P2 + P2.1 complete; final reconciliation audit done — authoritative test count 508 after the P2.1 financial-accuracy audit)
Verified by direct inspection of the working tree and of the real data files — not from
prior progress reports or provider marketing pages.

## What this project is

A personal quantitative research laboratory. Implemented scope is **daily NSE cash-equity
backtesting of order-based strategies** plus, since P2, a full **NIFTY index options research
layer** (derivatives normalisation, effective-dated reference DB, coverage registry, real
1m/5m/15m candle datasets, options cost/execution/accounting engines and a fail-closed
readiness gate). The options layer is **qualified for research modelling only**: strict
financial validation is blocked on the open cost-verification gate, and no options strategy
has been validated yet. P1 established what the zero-cost data can and cannot support:
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

## Test status (recorded 2026-11-05)

```
10 passed, 1 skipped in 18.83s   (P0 run 1 — `logs/pytest_baseline.txt`)
10 passed, 1 skipped in 14.02s   (P0 run 2 — reproducibility confirmed)
10 passed, 1 skipped in 11.21s   (P1 close-out re-run — tree still green)
300 passed, 1 skipped in 20.53s  (T9 — `logs/pytest_t9.txt`)
478 passed, 0 skipped in 196.44s (P2 stage 7 — superseded by the line below)
495 passed, 0 failed, 0 skipped in 84.78s  (P2 stage 8 + final reconciliation — superseded by the line below, `logs/pytest_p2.txt`)
508 passed, 0 failed, 0 skipped in 87.41s  (P2.1 financial-accuracy audit — AUTHORITATIVE, `logs/_p21_full_suite.log`)
```

Invoke it as `pytest` or `python -m pytest tests` from the repo root. Both are verified
green, and so is `python -m pytest` from inside `tests/`. `pytest.ini` (`testpaths =
tests`, `norecursedirs = vendor …`) is what makes a bare run safe: without it a bare
`pytest` also collected `vendor/quantjourney-bt/tests/`, which imports a package
(`backtester`) that is not installed here, and aborted with 8 `ModuleNotFoundError`
collection errors. **No file under `vendor/` was modified.**

Skip history: the single long-standing skip (`test_bracket_stop_target_and_risk_reward` —
`orders_12` produced no fresh BUY at the end of the 3-symbol smoke sample) was **eliminated
in P2 stage 7** by a deterministic conftest fixture; the suite has been at **zero skips**
since. The 478 → 495 step is the 17 Stage-8 readiness-gate tests being added, and 495 → 508 is
the P2.1 options-cost tests; no test was ever removed.

**Coverage (this was the real gap until T9).** The cost model, brokerage and statutory
charges, slippage, order fills, stop-loss/target execution, order priority, position
accounting, equity, look-ahead prevention, historical lot mapping, expiry mapping,
volume and OI conversion, timestamp handling, candle aggregation and the completeness
vocabulary are now all covered: **290 of the 300 passing tests are new in this milestone**
(231 for the data-accuracy items, 59 for the cost model and fill engine earlier in the same
milestone; the P1 baseline had only the 11 T8 signal tests). They run on the deterministic
fixtures in `tests/conftest.py`, never on the smoke dataset.
Remaining gap: `nifty_quant/validation/` statistics and `nifty_quant/reporting/` plots.

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

## Data-accuracy corrections outcome (measured, 2026-11-05)

Deliverables: `DATA_ACCURACY_CORRECTIONS.md` (the nine-part report), the
`nifty_quant/derivatives/` normalisation layer (`reference`, `units`, `timestamps`,
`candles`, `completeness`), `pytest.ini`, `requirements-audit.txt`, 9 re-runnable
`scripts/_m*.py` with their logs, and the T9 suite (300 tests).

Findings that bind all later work, in one line each:

1. **Provider volume is share-equivalents** = contracts × that row's lot. Exact to the share
   on the current lot-65 regime; verified over 310 file-days / 263 official dates.
2. **Provider OI changes scale on 2025-01-01**; the ×100 correction is labelled estimated and
   lifts post-break agreement with the official EOD figure from 0.00% to 73.60%. Only 67.52%
   of contract-days are independently verifiable, which caps any OI-based intraday feature.
3. **Timestamps are open-labelled** (`T` covers `[T, T+1)`), `15:30` is a closing print, and
   28 of 94 audited blocks are **rejected** rather than guessed.
4. **Completeness has four causes and they are now counted separately**: 9 provider holes, 1
   truncated expiry file, 3 truncated file-days, 28,754 missing contract-days, 13,591
   legitimate no-trade days.
5. **Reconciliation now covers all 318 free official dates**, not 21 — at no extra cost.

## Pending tasks

| ID | Task | State |
|---|---|---|
| P0 | Repo audit, git, docs, test record | **Done** |
| P1 | Zero-cost NIFTY options data verification + feasibility report | **Done** — awaiting review |
| — | Data-accuracy corrections (volume, OI, terminal candle, completeness) + T9 | **Done** — awaiting review (`DATA_ACCURACY_CORRECTIONS.md`) |
| P2 | Master accuracy remediation + readiness audit (8 stages) | **Done** — `P2_READINESS_AUDIT.md`, 8 atomic commits |
| P2.1 | Financial-accuracy audit of the options charge schedule (premium-turnover basis, official circulars) | **Done** — 11/15 rows verified; `transaction_costs_verified` stays fail-closed on 3 estimated pre-2026 exchange rows |
| T10 | Full-universe data fetch + 20-strategy research & validation | Outstanding |
| T11 | Strategy applicability screening → shortlist ~5 → keep ≤3 | Outstanding |
| — | FastAPI + DB layer + Streamlit dashboard (old plan t8 remainder) | Not started, not authorised |
| — | Options strategy validation run | Not started — engine exists and is tested; no strategy validated yet |

## Known limitations (honest list)

1. **Options capability is modelling-only for pre-2026 dates, verified-financial for the
   current era.** P2 added the contract reference, lot/expiry history, OI handling,
   cost/execution/accounting engines and real 1m/5m/15m datasets. P2.1 sourced 11 of 15
   charge-schedule rows from official NSE circulars / Finance Bill / pages (every
   *current-era* rate is verified); the three pre-2026 `exchange_txn` rows remain
   `estimated` (slab convention + inferred NSE/FA/64323 date), so trades on those dates are
   excluded from strict financial validation. No greeks and no order-book depth exist
   in any permitted source; nothing fabricates them.
2. **Daily only in practice.** `ResearchConfig.intervals = ["1d"]`. `orders_15` (5m)
   and `orders_16` (30m) are registered but there is no cached intraday data.
3. **Survivorship bias is exposed, not corrected.** The NIFTY50 constituents file is
   a *current* snapshot applied across 2010–2026 history. `run_metadata` records
   `survivorship_bias_exposed`.
4. **Zero validated strategies** — the research objective is currently unmet.
5. **Test coverage** — closed for the engine and the new derivatives layer in T9 (300
   tests). `nifty_quant/validation/` statistics and `nifty_quant/reporting/` plots are
   still untested.
6. Free-tier intraday history from yfinance is roughly 30–60 days for 5m bars and
   ~7 days for 1m bars (the provider's documented limit — **not re-measured in P1**, and
   NSE index-option chains were never queried through it); that alone cannot support
   2021-onwards 1-minute research. P1 confirmed no other permitted provider closes this —
   the Hugging Face archive is the only one with multi-year 1m option history.
7. **No 15-minute interval exists at any permitted provider** (Breeze documents
   `1minute/5minute/30minute/1day` only). 15m must be resampled from 1m.
8. **Option cost model is premium-turnover based and largely sourced (P2.1).**
   `nifty_quant/engine/options_costs.py` models the exchange transaction charge and the NSE
   IPFT contribution on *premium turnover, each side* (the fabricated `Rs 20 per lot` fee was
   removed), plus STT (three eras), SEBI, stamp duty, GST and slippage, effective-dated; the
   equity `costs.py` remains separate and untouched. 11 of 15 schedule rows are sourced from
   official documents, so a trade dated on/after 2026-03-01 with brokerage excluded or verified
   carries `strict_verifiable=True`; a trade on an estimated pre-2026 exchange row carries
   `strict_verifiable=False` and must not be presented as a verified financial fact.
9. **Terminal-bar convention — resolved, no longer open.** Timestamps are
   **open-labelled**: a bar stamped `T` covers `[T, T+1)`, and `15:30` is the
   closing/settlement print, not a candle (close == official `LastPric` for 100% of matched
   contracts; 0.038× a typical minute's volume; 83.3% single-priced). See
   `DATA_ACCURACY_CORRECTIONS.md` §3. 12 of 68 audited blocks carry stray labels past
   15:30 (up to 15:54) and are **rejected by strict validation, never guessed**; 5m/15m
   candles are produced only from `STRICT_OK` blocks.
10. **Only 67.52% of contract-days have a verifiable last-bar OI** (within 1% of the
    official end-of-day figure); 35.37% of bars carry provider OI == 0, and the lot size
    before 2024-01-02 is inferred from the archive itself with no official arbiter. Any
    OI-based intraday feature must be restricted to the verified observations.
11. **The provider archive has holes that are the provider's, not this repo's**: **9** listed
    NIFTY expiries inside the archive's own span (to 2026-07-02) have no file at all, the
    `2026-08-04` file stops mid-life on 2026-07-02 (byte size matches the provider's, so the
    download is not the cause), and 3 file-days are truncated (`2025-09-26` 15:00,
    `2025-10-21` 14:45, `2021-05-14` 09:15). Worse for planning: **41.89% of official
    contract-days have no provider minute bar at all**, 20,565 of them on contracts that
    traded more than 100 contracts — the archive's contract coverage is partial, so a
    universe must be derived from the archive, not from the exchange's listing.
    `nifty_quant/derivatives/completeness.py` names each cause separately; nothing is
    silently skipped. See §5 of `DATA_ACCURACY_CORRECTIONS.md`.

## P2 — Master accuracy remediation and readiness (complete)

P2 hardened the derivatives core and added four new subsystems, then produced the first real
1m/5m/15m NIFTY option candle datasets and closed with a readiness audit. Full detail and every
figure are in **`P2_READINESS_AUDIT.md`**; the fail-closed gate is
**`nifty_quant/validation/readiness.py`** (`scripts/_p2_readiness_checks.py`, `logs/p2_readiness.txt`).

- Effective-dated reference DB (`data/reference/*.csv`), a persistent expiry coverage registry
  (max observed coverage 37.81%, never extended), and a quantified 2026 OI-availability
  deterioration (33.6%→22.4% provider-observed) — all measured, never fabricated.
- 23 expiry files × 3 resolutions built to `data/derived/candles/` with **0** OHLC-containment and
  **0.000000** volume-conservation failures (`logs/p2_candle_integrity.txt`).
- Options cost engine + execution + symmetric accounting added; equity cost/fill/accounting paths
  unchanged (accounting extended behind a default-off `symmetric_realized` flag).
- Dependencies pinned exactly to the validated stack + `requirements.lock`; env check reports
  13 packages, 0 drift, REPRODUCIBLE.
- **Test suite: 508 passed, 0 failed, 0 skipped** (P2.1 re-run 2026-11-05; 495 at the P2
  reconciliation + 13 net new options-cost tests for the premium-turnover schedule, every rate
  boundary, IPFT, the brokerage-verified rule and the recalculated worked example) — the
  long-skipped bracket case runs on a deterministic fixture, which earlier surfaced and fixed a
  latent NaN-truthy bug in `signals/parsing.py` that had silently dropped every bracket take-profit.
- **Readiness verdict:** qualified for **research modelling** within the cached archive; **not**
  qualified for strict financial use because the single open gate is `transaction_costs_verified`
  (P2.1 sourced 11 of 15 rows and every current-era rate, but the three pre-2026 `exchange_txn`
  rows stay `estimated`, so the fail-closed gate remains open for the full horizon).

## Four-way status distinction (final reconciliation, 2026-11-05)

1. **Engineering implementation: complete.** All P2 subsystems built, tested (508 tests after
   the P2.1 financial-accuracy audit) and committed as 8 atomic stage commits plus the
   reconciliation and P2.1 commits (see `git log --oneline`).
2. **Research modelling: permitted** within the cached archive under the stated exclusions
   (estimated costs excluded from strict use, OI always an estimate, coverage ceilings honoured).
3. **Strict financial validation: blocked** — `transaction_costs_verified` open (P2.1 sourced
   11 of 15 rows and every current-era rate, but three pre-2026 `exchange_txn` rows stay
   estimated); the fail-closed gate refuses full-horizon qualification.
4. **Strategy validation: not yet completed** — no options strategy has run the validation
   gate; the equity smoke run's 0-of-6 is a scope artefact, not a quality verdict.

The denominator-by-denominator reconciliation of the coverage figures (17/313 expiries,
23 files per resolution, 37.81% ceiling, 41.82% vs 41.89% missing-data) is in
`P2_READINESS_AUDIT.md` §14.

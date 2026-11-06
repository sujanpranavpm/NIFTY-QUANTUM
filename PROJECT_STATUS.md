# PROJECT_STATUS.md

Last updated: 2026-11-07 (P0 + P1 + P2 + P2.1 + P2.1.2 + P2.2.3 + P2.2.4 + P2.2.5 + P2.2.5.1 + P2.2.6 + P2.2.6.1 + P2.2.6.2 + **P2.2.7 data-requirements & readiness audit** complete; authoritative test count 556 = 539 + 17 P2.2.6.2 exit-convention acceptance tests; P2.2.7 is **documentation-only** — the sole code addition is the read-only helper `scripts/_p227_data_audit.py`; NOT committed)
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

## P2.2.3 — common options-buying adapter (complete, tested, NOT committed)

Continues from `03a5d27`. New package `nifty_quant/options/` provides one reusable
pipeline (daily signal -> CE/PE + strike/expiry selection -> 15m option bars ->
premium ExitRules -> `OptionsExecutionEngine` -> `IndiaOptionsCommission` -> trade +
rejection records) for the five Tier-1 **long-only** strategies. **No strategy, engine, or
cost file was modified** — `git diff` on tracked files is empty; the signals are read by
running each strategy through the real `run_backtest`. Report:
`P2.2.3_OPTIONS_BUYING_ADAPTER.md`.

Verified facts (measured from code, not asserted from a doc):

- **DTE ruler settled:** index-session series (1,262 days, 2021-05-24..2026-07-02) is the
  ruler; official expiry calendar covers only 2024+ and collapses pre-2024 to DTE 0; the
  two agree 99/99 in the 2024+ overlap. **MIN_DTE=2 at entry-session D+1 => 121 eligible
  entry days** (84/18/19 dev/val/holdout); **only 13 are executable** (sparse option data).
- **Run (15m, 3 scenarios):** 39 trade rows (13 unique entries x 3) + 127 rejections; all
  `FILLED`; `option_type` = CE only; `dte_at_entry ∈ {2,4,5,6}`; `brokerage_excluded=True`
  on all 39; `statutory_verified` on 21/39 (2024+ dates), pre-Oct-2024 fail closed;
  `all_in_verified` on 0. Rejections: 116 `NO_OPTION_BARS_ON_ENTRY`, 9 `DTE_BELOW_MIN`,
  2 `ANCHOR_MISSING`. 5m sensitivity gives identical counts. Persisted: `artifacts/p223/`.
- **Tests:** `tests/test_options_adapter.py` = **23 passed** (10 required areas + 2
  integration smokes + 6 P2.2.6.2 exit-policy tests — 17 at the P2.2.3 milestone).
  **Full repo-root suite: 539 passed, 0 failed, 0 skipped**
  (516 baseline + 23 adapter).
- **Not done / not authorised:** no performance optimisation, no winners declared, no
  strategy edits, no live trading, no paid data, no dashboard/broker. **Nothing committed —
  awaiting explicit approval.**

## P2.2.5 — initial strategy screening (complete, tested, NOT committed)

Continues from the completed **P2.2.4** expanded working tree (265 usable expiries) and evaluates
the five Tier-1 candidates on the **unchanged, audited options adapter** against the pre-registered
methodology `P2.2_BACKTESTING_METHODOLOGY.md`. Harness `scripts/_p225_screen.py` calls
`OptionTradeAdapter` exactly as P2.2.3/P2.2.4 and wires the evaluation through the existing
`nifty_quant/validation/*` validators and `analytics.metrics.max_drawdown`. **No strategy, engine,
cost, threshold or floor was modified** — all periods, floors, statistics, baselines and the
decision rule are taken verbatim from the methodology; nothing was forced to qualify; the protected
holdout was read **once**, at the end, never used to decide. Report:
`P2.2.5_INITIAL_SCREENING_REPORT.md`.

**Outcome — zero candidates advance:**

| Candidate | Development unique entries (≥30 floor) | BASELINE Dev+Val expectancy | Classification |
|-----------|:-------------------------------------:|:---------------------------:|----------------|
| orders_12 | 8 | +₹235.1 | **Insufficient evidence** |
| orders_01 | 8 | +₹235.1 | **Insufficient evidence** |
| orders_18 | 8 | +₹235.1 | **Insufficient evidence** |
| orders_05 | 30 | −₹113.9 (PF 0.906, sign flips) | **Fails screening** |
| orders_07 | 28 | +₹161.3 (sign-stable) | **Insufficient evidence** |

* Binding constraint is now the **Development trade-count floor (≥30)**, not data availability —
  the expansion made the option calendar dense (only 19 rejections across all candidates).
* **Every candidate loses to the naive-ATM hold-to-expiry benchmark** (+6.06%/trade vs +2.35…+3.81%):
  on this data the fixed ±30% TP/SL exits destroy more ATM drift than they protect.
* **No candidate clears PSR/DSR ≥ 0.95**; Benjamini–Hochberg leaves all q = 1.0; walk-forward has
  **0/22 reliable OOS folds** (no fold reaches the ≥5-trade floor at this event density).
* All results **statutory-only, brokerage-excluded** (`all_in_verified` 0/363); pre-Oct-2024 rows
  modelling-only; no figure titled "net profit".
* `orders_12/01/18` produce a **byte-identical signal set** on the single-instrument daily harness
  (indistinguishable here; reported honestly, not a bug).
* **Full repo-root suite: 533 passed, 0 failed, 0 skipped** (no library code touched; 539
  since P2.2.6.2 added the 6 exit-policy tests). Reproduce:
  `$env:PYTHONPATH='.'; python scripts/_p225_screen.py`. Outputs gitignored under
  `artifacts/p225/` + `logs/p225_screen.{txt,json}`. **Not committed — awaiting review.**

## P2.2.5.1 + P2.2.6 — screening integrity audit and corrected exit replay (complete, NOT committed)

**P2.2.5.1 (audit).** The narrow integrity audit found a **governance defect**: the exit band
*run* in P2.2.3→P2.2.5 (**±30%/30%, uniform**) is **not** the band *approved* by the methodology
(**TP +15% / SL −8%**, ratio-preserving → `orders_12` **+16%/−8%**; sources: adaptation design
§L168-170/L248, reconciliation §3.4/§5). The substitution was introduced at the P2.2.3
adapter-implementation step, never approved, and mislabelled "unchanged" in the P2.2.5 report.
It was **not** holdout/look-ahead selection (fixed a-priori symmetric band). The audit also showed
the `n=855` naive-ATM benchmark is an **unconditional all-days series whose entries match no
candidate** (like-for-like bar = signal-matched hold-to-expiry, which it provides), and confirmed
the `orders_12/01/18` **byte-identical signal array**. Report: `P2.2.5.1_SCREENING_INTEGRITY_AUDIT.md`
(harness `scripts/_p2251_audit.py`; P2.2.5 artifacts preserved verbatim).

**P2.2.6 (corrected replay).** Controlled replay of the screening under the **approved band**, via a
config-level `ExitConvention` override only (`AdapterConfig(exit=…)` — library default and all
strategy/engine/cost code untouched; entry-set identity verified: sym-diff 0 vs the as-run and
hold legs for every candidate). Results (15m, BASELINE, Dev+Val, expectancy ₹): orders_12 **−66.4**,
orders_01/18 **−97.6**, orders_05 **−122.4**, orders_07 **−79.4** — the tighter asymmetric band is
*worse* than ±30% on every candidate and no candidate is break-even (vs four under the as-run band);
**classifications are unchanged** (4 × insufficient evidence, `orders_05` fails screening; zero
advance) because the Development ≥30 floor dominates. Signal-matched hold-to-expiry beats both TP/SL
bands on every candidate (+13.7%…+25.9%/trade). Statistics unchanged in kind: no PSR/DSR ≥ 0.95,
all BH q > α, 0/22 reliable OOS folds. Holdout rows recomputed but marked **supplemental** (already
disclosed in P2.2.5 — not pristine confirmation, used for nothing). The going-forward exit
convention is an **open user decision** (report §10: adopt +15/−8, ratify ±30%, or amend the exit
structure). Report: `P2.2.6_EXIT_RESEARCH_REPORT.md`; harness `scripts/_p226_exit_replay.py`;
artifacts under `artifacts/p226/` (gitignored); logs `logs/p226_exit_replay.{txt,json}`.
**Full repo-root suite re-run green; nothing committed — awaiting review.**

## P2.2.6.1 — forensic results reconciliation (complete, NOT committed)

The P2.2.6 *session summary* contradicted the persisted artifacts (a claimed +15%/−15%
band, invented PF "gates", an "orders_12 → Advances" line, a spurious +11,060 hold figure).
`scripts/_p2261_reconcile.py` rebuilt every headline metric **independently from the raw
per-trade rows** (own metric code, no helper imports, no adapter re-run; read-only).
Findings: **four** distinct exit conventions were ever executed (±30/30; +15/−8; orders_12
+16/−8; hold) — a +15%/−15% run **does not exist** in any `summary.json`, payload, script or
artifact name. Cross-check vs published tables: **0 mismatches**; per-row
`net == gross − costs` identity holds (0 violations of 363+363+33); **no negative PF cell
anywhere** (PF is Σwins/Σ|losses| ≥ 0 by construction — a summary "−24 PF" conflated PF
with an ₹ expectancy); exit-comparison entry-set identity proven (sym-diff 0, quantities
identical, all five candidates); ₹ vs premium-denominated expectancy can legitimately carry
opposite signs (different denominators — §5 gate reads expectancy_net in ₹). Classification
re-derived from raw rows under the unchanged pre-registered rule (15m primary, Dev+Val,
approved band; 5m sensitivity only; holdout supplemental): **identical to both published
decision sets — zero advance** (12/01/18/07 Insufficient evidence, 05 Fails screening).
Report: `P2.2.6.1_RESULTS_RECONCILIATION.md` (one clean table per configuration + final
classification + contradicted-claims register); outputs `logs/p2261_reconcile.{txt,json}`,
`logs/_p2261_run.log`, `logs/_p2261_5m_check.log`; all original artifacts preserved; no
threshold/strategy change; suite re-run green; **no winner declared — nothing committed**.

## P2.2.6.2 — exit-convention decision & default hardening (complete, NOT committed)

User-authorised decision executed: **+15%/−8% is now the actual default** options research
exit convention, with orders_12's ratio-preserving **+16%/−8%** preserved; ±30%/30% retained
**only** as the pinned historical configuration; hold-to-expiry stays a research benchmark,
never a default. Library changes (only `nifty_quant/options/pipeline.py` + `__init__.py`):
`ExitConvention` defaults → 0.08/0.15; new central `EXIT_POLICY` map + `approved_exit_for()`
(unknown strategy id → `ValueError`); `AdapterConfig.exit` is now `Optional` — `None` applies
the policy **per strategy automatically** (multi-strategy runs no longer need the
two-invocation orders_12 workaround), an explicit convention overrides it; construction-time
validation rejects NaN/negative/non-numeric fractions; empty strategy sets rejected; run
summaries record the band each strategy actually used (`exit_convention_per_strategy`,
`exit_policy_applied`; uniform runs keep the historical flat-dict shape). Historical scripts
`_p223_run_adapter/_p2231_audit/_p224_reconcile/_p225_screen` now pin
`ExitConvention(0.30, 0.30)` explicitly ("do not update" comments); `_p2251_audit`'s
`asrun_exit` made explicit; `_p226_exit_replay`/`_p2261_reconcile` already band-explicit —
untouched. **No artifact regenerated or touched** (p223–p226 mtimes predate this session); no
strategy/engine/cost/threshold change; verdict unchanged — zero candidates advance. Tests: +6
(exit-policy section in `tests/test_options_adapter.py`), full suite **539 passed**
(`logs/_p2262_fullsuite.log`). Decision record: `P2.2.6.2_EXIT_CONVENTION_DECISION.md`.
**Not committed — awaiting review.**

## P2.2.7 — complete data requirements & readiness audit (complete, documentation-only, NOT committed)

Before any new source is searched or downloaded, this milestone establishes **exactly what data
NIFTY QUANT needs** to become a comprehensive quant research platform, and what it actually has —
from **direct inspection of the working tree and the real files**, not from prior summaries. A new
read-only helper `scripts/_p227_data_audit.py` (→ `logs/_p227_data_audit.log`) re-measured every
dataset fresh; **no dataset was modified and nothing was downloaded.**

Deliverables: `P2.2.7_DATA_REQUIREMENTS_SPECIFICATION.md` (master inventory A–G + 7 requirement
classes + governance rules), `P2.2.7_CURRENT_DATA_COVERAGE_AUDIT.md` (actual paths, schemas, fresh
counts, six-way status), `P2.2.7_DATA_GAP_AND_BLOCKER_REPORT.md` (missing→capability impact;
absolute blockers vs quality improvements), `P2.2.7_RESEARCH_READINESS_MATRIX.md` (GREEN/AMBER/RED
verdict per capability).

Ground-truth facts re-measured this session (supersede older counts):

- **Provider raw** `data/raw/p1_sample`: 1-minute NIFTY **index** 486,050 rows / 1,262 days
  (2021-05-24→2026-07-02); **267** option-expiry files (by year 2021:31, 2022:52, 2023:52, 2024:52,
  2025:52, 2026:28) — the archive is a **dense weekly/monthly set now**, not the old 23-file sample.
- **Official arbiter** `data/raw/p1_nse`: 318 NSE F&O bhavcopies, **2024-01-01→2026-11-03 only**
  (verification floor 2024-01-02; pre-2024 not served).
- **Derived candles** `data/derived/candles`: **801 = 267 expiries × {1m,5m,15m}** (no 60m/30m).
- **Registry** `nifty_coverage_registry.csv` 181,973 rows; arbiter-verified 179,646; by-year
  contract-day cells 2021:345 / 2022:492 / 2023:532 **all UNKNOWN (arbiter=0)**, 2024:95,433 /
  2025:68,751 / 2026:16,107 (arbiter-verified). PRESENT 77,627; ceiling ~37.81% (arbitrated only).
- **Equity cache** `data/cache/1d`: `IDX_NSEI` 4,133 daily (2010-01-04→2026-10-30); full NIFTY50
  **not fetched** (6-symbol smoke, 2 rejected); `nifty50_snapshot.csv` membership_from/to empty →
  `survivorship_bias_exposed=True`.

Verdict (honest): the **Tier-1 long-only CE option study runs but is quality-bounded** (short-DTE
density ⇒ "insufficient evidence"); **2021-2023 option data is present-but-unverifiable**; **full-horizon
strict financial validation is permanently fail-closed** (2 slab-era cost rows estimated). **Absolutely
blocked at ₹0:** bid/ask/spread/depth execution realism, observed IV/Greeks/VIX, IV-aware strategies.
**Closable later (no new data or a ₹0 fetch):** real liquidity gate + `SttlmPric` wiring (behaviour fixes,
deferred by the no-execution-change rule), denser option history, and a future-authorised full-universe +
point-in-time equity fetch (T10). Monte Carlo is **not implemented**; 10,000 iterations are repetitions of
a resample of the finite observed set, not 10,000 market observations. Suite **556 passed**; **nothing
committed — awaiting review before external-source discovery or any download.**

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
508 passed, 0 failed, 0 skipped in 87.41s  (P2.1 financial-accuracy audit — superseded by the line below, `logs/_p21_full_suite.log`)
516 passed, 0 failed, 0 skipped in 38.44s  (P2.1.2 follow-up — AUTHORITATIVE, `logs/_p212_full_suite.log`)
```

Invoke it as `pytest` or `python -m pytest tests` from the repo root. Both are verified
green. `python -m pytest` from inside `tests/` also passes (513 passed) but reports 3
data-anchored skips because those tests resolve `data/derived/` and `logs/` relative to the
current directory — the repo-root invocations are the official ones. `pytest.ini` (`testpaths =
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
| P2.1.2 | Follow-up: primary circular NSE/FA/64232 sourced (FA73061's "64323" is a circular-reference mismatch, likely a typographical error); statutory vs all-in verification levels separated | **Done** — 12/15 rows verified; gate stays fail-closed on the 2 slab-era exchange rows (516 tests) |
| P2.2.5 | Initial strategy screening of the five Tier-1 candidates on the expanded 265-expiry dataset | **Done** — zero advance (4 insufficient evidence, 1 fails screening); every candidate loses to naive-ATM; floors/holdout untouched; 533 tests |
| P2.2.5.1 | Narrow screening-integrity audit (TP/SL discrepancy, naive-ATM benchmark, identical signals) | **Done** — unapproved ±30% band found (approved +15%/−8%); all-days ATM bar not like-for-like; 12/01/18 signal identity confirmed; nothing changed |
| P2.2.6 | Corrected screening replay under the approved exit band + signal-matched hold comparison | **Done** — classifications unchanged (zero advance); approved band worse than ±30% on every candidate; hold-to-expiry dominates; exit convention decision referred to user |
| P2.2.6.1 | Forensic reconciliation of the contradictory P2.2.6 session-summary claims (config inventory, independent metric recompute, PF/definition audit) | **Done** — artifacts consistent (0 mismatches, no negative PF, no +15/−15 run existed); classifications unchanged, zero advance; contradicted-claims register delivered |
| P2.2.6.2 | Exit-convention decision & default hardening (user-authorised): +15/−8 default, orders_12 +16/−8 via central `EXIT_POLICY`, ±30% pinned historical-only | **Done** — library default changed with historical scripts pinned; +6 tests (539 green); no artifacts touched; verdict unchanged (zero advance) |
| P2.2.7 | Complete data-requirements & readiness audit (documentation-only): master inventory A–G, per-dataset spec, fresh coverage audit of the real files, gap/blocker analysis, readiness matrix | **Done** — 4 deliverables + STATUS/HANDOFF; sole code change is read-only helper `_p227_data_audit.py`; no download/account/provider discovery; no dataset modified; MC not implemented; suite 556 green; nothing committed — awaiting review before external-source discovery |
| T10 | Full-universe data fetch + 20-strategy research & validation | Outstanding |
| T11 | Strategy applicability screening → shortlist ~5 → keep ≤3 | Outstanding |
| — | FastAPI + DB layer + Streamlit dashboard (old plan t8 remainder) | Not started, not authorised |
| — | Options strategy validation run | Not started — engine exists and is tested; no strategy validated yet |

## Known limitations (honest list)

1. **Options capability is modelling-only for pre-October-2024 dates, verified-financial
   from 2024-10-01 onwards.** P2 added the contract reference, lot/expiry history, OI handling,
   cost/execution/accounting engines and real 1m/5m/15m datasets. P2.1 sourced the
   charge-schedule rows from official NSE circulars / Finance Bill / pages and P2.1.2 added
   the primary circular NSE/FA/64232 (12 of 15 rows verified); the two *slab-era*
   `exchange_txn` rows (pre-2024-10-01) remain `estimated` because the marginal slab for a
   research book is a convention, so trades on those dates are excluded from strict financial
   validation. No greeks and no order-book depth exist
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
8. **Option cost model is premium-turnover based and largely sourced (P2.1 / P2.1.2).**
   `nifty_quant/engine/options_costs.py` models the exchange transaction charge and the NSE
   IPFT contribution on *premium turnover, each side* (the fabricated `Rs 20 per lot` fee was
   removed), plus STT (three eras), SEBI, stamp duty, GST and slippage, effective-dated; the
   equity `costs.py` remains separate and untouched. 12 of 15 schedule rows are sourced from
   official documents, so a trade dated on/after 2024-10-01 with brokerage excluded or verified
   carries `strict_verifiable=True`; a trade on an estimated slab-era exchange row carries
   `strict_verifiable=False` and must not be presented as a verified financial fact.
   P2.1.2 also separates the verification levels: `statutory_verified` (all statutory rates
   sourced, brokerage irrelevant) vs `all_in_verified` (statutory **and** an attested broker
   fee) — brokerage *exclusion* is never all-in verification, and `cost_basis` labels such a
   result "statutory-only … do not label as net profit".
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
- **Test suite: 516 passed, 0 failed, 0 skipped** (P2.1.2 re-run 2026-11-06; 508 after P2.1,
  +8 net new tests pinning the FA64232 era and the statutory/all-in verification levels) — the
  long-skipped bracket case runs on a deterministic fixture, which earlier surfaced and fixed a
  latent NaN-truthy bug in `signals/parsing.py` that had silently dropped every bracket take-profit.
- **Readiness verdict:** qualified for **research modelling** within the cached archive; **not**
  qualified for strict financial use because the single open gate is `transaction_costs_verified`
  (P2.1.2 sources 12 of 15 rows and every rate in force from 2024-10-01, but the two slab-era
  `exchange_txn` rows stay `estimated`, so the fail-closed gate remains open for the full horizon).

## Four-way status distinction (final reconciliation, 2026-11-05)

1. **Engineering implementation: complete.** All P2 subsystems built, tested (516 tests after
   the P2.1.2 follow-up) and committed as 8 atomic stage commits plus the
   reconciliation, P2.1 and P2.1.2 commits (see `git log --oneline`).
2. **Research modelling: permitted** within the cached archive under the stated exclusions
   (estimated costs excluded from strict use, OI always an estimate, coverage ceilings honoured).
3. **Strict financial validation: blocked** — `transaction_costs_verified` open (P2.1.2 sources
   12 of 15 rows and every rate in force from 2024-10-01, but the two slab-era `exchange_txn`
   rows stay estimated); the fail-closed gate refuses full-horizon qualification.
4. **Strategy validation: not yet completed** — no options strategy has run the validation
   gate; the equity smoke run's 0-of-6 is a scope artefact, not a quality verdict.

The denominator-by-denominator reconciliation of the coverage figures (17/313 expiries,
23 files per resolution, 37.81% ceiling, 41.82% vs 41.89% missing-data) is in
`P2_READINESS_AUDIT.md` §14.

## P2.1.2 — FA64232 sourcing and verification-level split (complete, 2026-11-06)

A follow-up to the P2.1 charge audit, resolved at ₹0 and committed atomically. Two things
changed:

1. **The Sept-2024 uniform-rate circular was found.** P2.1 had left the 2024-10-01
   `exchange_txn` row `estimated` because FA73061 cites it as `NSE/FA/64323` and that PDF
   returns 404 — a circular-reference mismatch, likely a typographical error. The official
   primary document located is **`NSE/FA/64232`** (dated September 27, 2024), matching the
   subject and effective charges referenced by FA73061. Fetched and cached (`scripts/_p21_charge_sources.py`,
   `data/raw/p21_charges/FA64232.pdf`, verbatim bytes in `logs/p21_excerpts_FA64232.txt`):
   *"Equity Options: Rs. 35.03 each side per lakh of premium value … applicable with effect
   from October 1, 2024"* = Rs 3,503/cr = 0.03503%. The 2024-10-01 row is now
   `verification=verified`, and the 2021 slab values were sourced from **FA46730**. That
   lifts the schedule to **12 of 15 rows verified** and makes every trade dated on/after
   2024-10-01 strict-verifiable (brokerage excluded). The **two slab-era `exchange_txn` rows
   stay `estimated`**: the slab *values* are sourced but the marginal slab a member pays is
   unknowable for a research book, so the fail-closed `transaction_costs_verified` gate
   remains open for the full horizon (11/12 gates pass per resolution).
2. **Two verification levels were separated that P2.1 had conflated.** A result now carries
   `statutory_verified` (every statutory rate sourced — brokerage irrelevant) and
   `all_in_verified` (statutory **and** an *attested* broker fee). Crucially, *excluding*
   brokerage (`model='none'`) is **not** all-in verification — the broker fee is a real cost
   that has been omitted, not a cost that has been verified — so such a trade is
   `statutory_verified=True, all_in_verified=False`. `strict_verifiable` is kept as the
   backward-compatible "safe to publish in this configuration" verdict (statutory sourced AND
   brokerage excluded-or-verified), and a new `cost_basis` label refuses to call an
   excluded-brokerage result a "net" figure. Worked 2025 round-trip recalculates to
   **Rs 48.4865** (Rs 16.1886 slab-era 2022 comparison fails closed).

**Suite: 516 passed, 0 failed, 0 skipped** (`logs/_p212_full_suite.log`) — 8 net new tests
over P2.1's 508 pin the FA64232 era and the statutory/all-in levels; no test removed. Three
tests that had asserted the *old* "2025 is estimated" semantics were re-targeted to a
slab-era date (their fail-closed intent is preserved; a new test pins the 2025-now-verified
fact). From `tests/` the suite still reports 3 disk-anchored skips (those tests read
`data/derived` and `logs/` by CWD) — unchanged from before P2.1.2 and why the repo-root
invocation is the official one.

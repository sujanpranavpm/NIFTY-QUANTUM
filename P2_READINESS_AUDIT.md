# P2 Readiness Audit — NIFTY QUANT Master Accuracy Remediation

**Scope:** the 13-part acceptance report closing P2 (directive Parts 10–11).
**Machine:** python 3.12.10, pandas 3.0.1, numpy 2.4.3, pyarrow 25.0.1, scipy 1.17.1, pytest 9.1.1.
**Generated:** 2026-11-05. Every figure below is reproduced from a committed artifact or a
captured run log, cited inline. This audit **authorises** P2 data ingestion; it does **not**
run ingestion, optimise a strategy, build a dashboard, or place a live order.

Standing guardrails that held across all eight stages: raw provider columns are never
overwritten; no bar, rate or contract is fabricated; no gap is silently filled; no EOD
look-ahead; `vendor/` untouched; no new accounts or subscriptions (₹0); `logs/`, `data/raw/`,
`data/cache/`, `data/derived/`, `data/options/` stay git-ignored; `data/reference/` is tracked.

---

## 1. Open-interest integrity

**Verdict: honest, measured, described as an estimate — never "fully accurate".**

- Genuine-zero vs unavailable is preserved: a missing OI observation stays missing — there is
  **no missing→zero coercion and no forward/backward fill** (`nifty_quant/derivatives/units.py`;
  asserted in `tests/test_data_units.py`).
- The independent verification column `oi_verified` is populated only where a converted
  last-bar OI matches the official EOD within 1%, and only for an exact
  `(trading_day, expiry, strike, option_type)` join at/after 15:30. Unverified rows stay NaN
  (`oi_verified_flag` boolean), never filled.
- Reconciliation evidence (`logs/m3_oi_norm.txt`) shows the scale change: raw ratio ≈ 1.0 in the
  `share_equivalent` era and ≈ 100.0 in the `one_hundredth_share_equivalent` era from 2025-01-01,
  with the converted value landing within 1% of official (e.g. 2025-01-02 converted 0.9988).
- Availability deterioration is **quantified, not asserted** (`logs/p2_oi_availability.txt`):
  provider-observed OI on exchange-positive contract-days is **33.6% pre-2026 vs 22.4% in 2026**
  (a +11.2pp adverse change); independently verified is **22.7% pre-2026 vs 13.1% in 2026**.
- Tests: `tests/test_data_units.py` pins raw-preserved, date-aware ×100 `estimated`, and the
  verified/unverified/missing/zero separation; the EOD-visibility gate is tested.

## 2. Volume conversion

**Verdict: contract-level conversion by each row's own board lot; unit problems are visible, not hidden.**

- Provider volume is share-equivalents = contracts × board lot (`logs/m2_volume_units.txt`
  unit conclusion); conversion divides by **that contract's own lot on that day**
  (`NewBrdLotQty` per row), not a date-only fallback.
- Mean exact share-equivalent match 46.37%; only **0.834%** of minute bars are not a whole
  multiple of the lot; the opening-bucket `lot*k+1` artefact is tolerated but
  `volume_integral=False` is kept visible (never silently rounded).
- A multi-lot day (61 such dates exist) **refuses** a single-lot-per-day assumption for realised
  P&L; `observed_lots_on(day)` with >1 entry raises rather than guessing (`units.py`).
- Partial/truncated sessions are flagged and excluded from strict full-session statistics.
- Evidence: `logs/m2_volume_units.txt` (310 file-day records); tests in `tests/test_data_units.py`.

## 3. Historical contract coverage

**Verdict: every expected and observed contract-day is classified; nothing is silently skipped.**

- The persistent expiry coverage registry (`data/reference/nifty_coverage_registry.csv`,
  69,066 rows) records the full vocabulary. Contract-day counts (`logs/p2_coverage.txt`):
  MISSING_CONTRACT 41.82%, PRESENT 30.79%, NO_TRADE 19.77%, PRESENT_WITHOUT_OI 6.96%,
  TRUNCATED_SESSION 0.49%, UNKNOWN 0.17%, MISSING_CANDLE 1 (never aggregated).
- **Maximum observed coverage 37.81%, measured and never extended**; expiries present 17 of 313;
  10,708 unverifiable provider cells (2021–2023, no arbiter) reported as such; 20,431 cells with
  candle minute-gaps detected, not fabricated; 17 of 26,293 high/low cells exceed the official
  range and are flagged (None where no official range exists).
- Reconciles to the completeness report: `reconcile vs logs/m5_completeness.json: MATCH`.
- A silent-skip guard raises if any MISSING contract carries no reason
  (`CoverageRegistry.assert_no_silent_skip`, tested in `tests/test_coverage_registry.py`).
- Alt-source assessment (`scripts/_p2_alt_sources.py`, `logs/p2_alt_sources.txt`): no free,
  legally-usable, no-account source extends intraday history beyond the cached archive →
  explicit fail-closed restriction, no purchase, no account.

## 4. Expiry & lot reference (effective-dated)

**Verdict: one effective-date-aware store; today's spec is never applied to a historical contract.**

- `data/reference/` holds `nifty_reference_parameters.csv`, `nifty_lot_history.csv`,
  `nifty_expiry_dates.csv`, `nifty_trading_days.csv`, `nifty_strike_intervals.csv`,
  `nifty_tick_sizes.csv`, `nifty_expiry_cadence.csv`, `nifty_coverage_registry.csv` — keyed
  `(parameter, effective_from, effective_to, value, provenance, estimated)`.
- Rebuilt deterministically from cache (`logs/p2_reference.txt`): 318 trading days derived from
  bhavcopy dates that actually traded (holidays are not cached — absence-of-report on a weekday
  is the honest derived signal, documented as derived, not an NSE circular); 61 mixed-lot dates
  handled explicitly; 184 listed expiry dates (2021-05-27 .. 2031-06-24).
- Board lot: 9 windows, each labelled `[verified]` (NewBrdLotQty observed on N bhavcopy dates) or
  `[ESTIMATED]` (no arbiter before 2024-01-02). Tick 0.05 **verified 100% on-grid across the whole
  cached archive and both sides of every lot transition**, not from two samples. Strike interval
  1000 → 500 (2026-01-05) → 50 (2026-07-01), each measured from modal StrkPric gaps.
- Expiry weekday regime change: **Thursday until 2025-08-31, Tuesday from 2025-09-01**
  (circular NSE/FAOP/68747); identity via `expiry_from_iso`, never assuming one weekday across
  years; ambiguous contract mappings raise. Tests: `tests/test_reference_db.py`,
  `tests/test_lot_reference.py`.

## 5. Timestamp convention

**Verdict: `open_labelled` resolved and enforced; the 15:30 print is preserved separately.**

- `nifty_quant/derivatives/candles.CONVENTION == "open_labelled"`; aggregation refuses to run
  without a resolved convention (`RuntimeError`).
- The 15:30 closing print is kept as its own raw row at 1m and folded (raw value preserved in a
  dedicated `closing_print` column) into the terminal in-session candle at 5m/15m via
  `CLOSING_PRINT_INDEX` clamped to `LAST_BUCKET_INDEX` — no invented candle, no misattributed
  volume.
- Strict session-boundary and duplicate-timestamp validation; ambiguous blocks are rejected, not
  guessed. Evidenced in `logs/p2_candle_integrity.txt` (print_placement n/n on every file) and
  `tests/test_candle_generation.py`.

## 6. 1m / 5m / 15m aggregation

**Verdict: real datasets exist on disk and pass integrity with zero containment/volume failures.**

- `data/derived/candles/` holds 23 files each for 1m, 5m and 15m (69 parquet files).
- Grand totals (`logs/p2_candle_integrity.txt`): 1m rows written 7,272,105; coarse candles
  2,056,777; **OHLC containment failures 0**; volume conservation delta **0.000000**;
  closing-print mismatches 0; files failing integrity **0 / 23**; **INTEGRITY VERDICT: PASS**.
- 251 (day × expiry) blocks rejected were reported, never aggregated; only `STRICT_OK` blocks are
  aggregated and every rejection is written to a rejection table with its verdict/reason.
- 1m canonical = raw OHLCV+OI pass-through (raw never mutated); 5m→75, 15m→25 buckets; empty
  intervals are not filled; NaN OI preserved; volume conserved. Tests:
  `tests/test_candle_generation.py`, `tests/test_missing_data_behaviour.py`.

## 7. Options cost model

**Verdict: dedicated effective-dated engine; every unverified rate is estimated and excluded (fail-closed).**

- `nifty_quant/engine/options_costs.py` (`IndiaOptionsCommission`) computes brokerage (neutral
  default — **no broker assumed without configuration**), STT on premium (sell side), exchange
  transaction charge (per lot), SEBI, GST (18% on brokerage+exchange+SEBI), stamp duty (buy leg),
  and slippage, on premium × lot_size × lots, **entry and exit separately**, partial exits
  prorated. Exports a per-trade breakdown mirroring the equity model (detail excludes `total`).
- The schedule `data/reference/options_charge_schedule.csv` is effective-dated. The offline audit
  (`logs/p2_options_cost.txt`, `scripts/_p2_cost_schedule_audit.py`) found **0 of 5 statutory
  rates** present in cached circulars/spec pages → all 5 flagged `estimated=True`.
- Worked round-trip (lot 75, buy 22,500 / sell 27,000 turnover): BUY leg Rs 47.9016, SELL leg Rs
  74.2319, **round-trip Rs 122.1334**, `strict_verifiable: False` → **excluded from strict
  financial validation**, usable for modelling only. The equity `costs.py` is untouched (additive).
- Tests: `tests/test_options_costs.py` (component-by-component against a hand-computed trade,
  effective-date boundaries, entry/exit asymmetry, partial-exit proration, unverified excluded).

## 8. Execution simulation

**Verdict: next-bar, collision-conservative, gap-refusing; three scenarios reported; optimist alone never qualifies.**

- `nifty_quant/engine/options_execution.py` models BUY CE/PE with premium entry/exit, stop-loss,
  target, trailing stop, time exit and expiry exit. **No automatic fill at the signal-candle
  close; earliest entry is the next bar.** Configurable adverse slippage (buy ×(1+bps), sell
  ×(1−bps)); stop fills `min(stop, open)` on a gap; target fills at the limit exactly (no
  favourable-gap credit); **stop wins a same-bar collision** (conservative); trailing anchors from
  prior bars only; fills during missing-data intervals are rejected (entry → NO_ENTRY, expiry gap
  → UNRESOLVED); liquidity gate can refuse an exit (OPEN_AT_END). Never claims exchange execution.
- Three explicit scenarios A OPTIMISTIC / B BASELINE / C CONSERVATIVE (`logs/p2_execution.txt`):
  net P&L on a rising target path **A 2,887.11 ≥ B 2,880.51 ≥ C 2,870.62** (monotonic);
  collision resolved to STOP (net −1,614.08); missing-data expiry gap → UNRESOLVED (no invented
  price). A strategy profitable only under A is flagged fragile; qualification is **not granted on
  the optimistic result alone**. `config.py` `ExecutionScenario` presets drive it.
- Tests: `tests/test_options_execution.py` (19 cases).

## 9. Accounting verification

**Verdict: symmetric premium-based P&L from real quantities; the equity path is bit-for-bit unchanged.**

- `nifty_quant/engine/options_accounting.py` (`OptionsLedger`) handles historical and multiple lots,
  premium outlay, cash availability, entry/exit charges from the Stage-5 engine, realised P&L =
  premium movement × contract quantity − costs (costs assessed once), unrealised P&L, scale-in
  weighting, partial exits, and peak max-capital exposure. Guards prevent over-close, duplicate
  exit, side-flip, non-positive/non-finite amounts and back-dated timestamps. Long **and** short
  realised gains are credited (symmetric), unlike the equity default.
- `nifty_quant/engine/accounting.py` gained a **default-off** `symmetric_realized` flag, so the
  pinned equity limitation (long-side-only) stays identical — verified by the pre-existing
  `test_position_accounting.py` assertions and three added symmetric-mode tests.
- `strict_verifiable` flips to False whenever an estimated rate is used, tying accounting back to
  the cost gate. Tests: `tests/test_options_accounting.py` (19 cases).

## 10. Dependency & reproducibility

**Verdict: exact-pinned validated stack with a lockfile; a clean suite reproduces identically.**

- `requirements.txt` / `requirements-audit.txt` replaced the old `<3.0` pandas range with exact
  pins; `requirements.lock` freezes the direct stack (13 packages) under a CPython 3.12.10 header.
  Audit-only deps (`curl_cffi`, `pypdf`) stay separate; no blind upgrade; no `vendor/` collection.
- `scripts/_p2_env_check.py` verifies installed versions against the lock
  (`logs/p2_env.txt`): **13 packages, 0 drift, interpreter ok — verdict REPRODUCIBLE.**
- Same config → identical equity curve, lossless config round-trip, and identical options-execution
  results are asserted in `tests/test_reproducibility.py`.

## 11. Full pytest results

- **495 passed, 0 failed, 0 skipped** in 84.78s (`logs/pytest_p2.txt`, reconciliation re-run
  2026-11-05), from the documented project command at repo root. The **478** figure quoted at
  the Stage-7 commit was correct *at that commit*: Stage 8 then added the 17 readiness-gate
  tests (`tests/test_readiness_gating.py`), taking the suite to 495. Nothing was removed —
  478 + 17 = 495, the same suite plus the Stage-8 file.
- Discovery parity confirmed: `pytest`, `python -m pytest` at root, and from `tests/` all collect
  the identical **495 tests** (the conftest pins the repo root onto `sys.path`).
- The long-`skipped` bracket case `test_bracket_stop_target_and_risk_reward` now runs on a
  deterministic `conftest` fixture (an engineered single golden cross), reaching **zero skips**.
  Building that fixture exposed and fixed a latent bug in `nifty_quant/signals/parsing.py`:
  `stop_price or limit_price` treated a pandas `NaN` as truthy and silently dropped every bracket
  take-profit (and stop price from the exit description). Type-selected `_order_price` now reads the
  correct field; the live bracket resolves the published **2:1 R/R** (`rr = 1.9967`).
- New Stage-7 modules cover the validation package, the reporting package, reproducibility and
  missing-data behaviour; the two Stage-6 option modules and the Stage-8 readiness gate add the rest.

## 12. Remaining data limitations (stated honestly)

- **Cost verification is the open gate.** 0 of 5 statutory derivative rates are sourced from
  cached bytes; all are `estimated`, so options trades are **excluded from strict financial
  validation** (fail-closed) and usable for modelling only.
- **Coverage ceiling is 37.81%** of exchange-enumerated contract-days; 2021–2023 has no OI arbiter
  (10,708 unverifiable cells); single-source 2021–2023.
- **OI availability is an estimate and deteriorates in 2026** (22.4% provider-observed, 13.1%
  verified); never forward-filled or extended.
- **Intraday horizon is the cached archive** (newest expiry 2026-08-04 is itself a truncated
  tail); no free, no-account source extends it → reported UNKNOWN/MISSING, left unavailable.
- **No market depth, no greeks, no order-book data** were ever present; nothing here fabricates them.

## 13. Final P2 readiness assessment

- The fail-closed gate `nifty_quant/validation/readiness.py` + `scripts/_p2_readiness_checks.py`
  evaluates **12 qualification gates per resolution** (`logs/p2_readiness.txt`):
  **11 of 12 PASS for 1m, 5m and 15m**; the single open gate is `transaction_costs_verified`.
- Qualification is **separate per resolution**: a built 1m dataset never implies 5m/15m
  (`RESOLUTION_DATA_EVIDENCED` is checked independently), which `tests/test_readiness_gating.py`
  pins, together with "no evidence qualifies nothing", "a truthy-but-not-boolean value does not
  open the gate", "a typo cannot manufacture a qualification", holdout protection and
  resolution-agnostic walk-forward folds.
- **Conclusion:** the NIFTY data + backtesting layer is **qualified for research modelling**
  within the cached archive under the stated exclusions. It is **NOT qualified for strict
  financial use** because the derivative cost-verification gate remains open (estimated statutory
  rates) — exactly as the directive requires the system to fail closed rather than overclaim.
- P2 ingestion is authorised only for non-strict research. The next real step (out of P2 scope) is
  to source official derivative charge circulars to flip `transaction_costs_verified`, or accept
  the estimated-cost exclusion permanently for options strategies.

---

## 14. Reconciliation appendix (final consistency audit, 2026-11-05)

**Test count.** The authoritative count at HEAD is **495 passed, 0 failed, 0 skipped**
(`logs/pytest_p2.txt`, re-run for this appendix). Documents quoting 478 refer to the Stage-7
commit, before the 17 Stage-8 readiness tests existed; 478 + 17 = 495 and no test was ever
deleted. Every report now cites 495.

**Coverage denominators (why four different figures are all correct).**

| Figure | Numerator | Denominator | Meaning |
|---|---|---|---|
| 17 of 313 expiries | 17 expiry-level `PRESENT` rows | 313 expiry-level registry rows = 178 exchange-listed (cached bhavcopy, arbiter floor 2024-01-02) + 135 provider-published expiries outside the arbiter window (pre-2024, `UNKNOWN`) | Expiries with a cached provider file *and* an official arbiter |
| 23 files per resolution | 23 cached provider expiry files on disk (17 arbitrated + 6 pre-2024) | n/a — an inventory, not a ratio | Each cached file is aggregated to 1m/5m/15m → 69 parquet files; the 6 pre-2024 files are built as raw pass-through but stay flagged unverifiable (they supply the 10,708 unverifiable cells) |
| 37.81% max observed coverage | 25,954 present contract-days (`PRESENT` 21,168 + `PRESENT_WITHOUT_OI` 4,786) | 68,638 exchange-enumerated arbitrated contract-days | Contract-days where the provider actually has candles; `NO_TRADE`, `TRUNCATED_SESSION` and missing rows are not counted as coverage |
| 41.82% `MISSING_CONTRACT` | 28,754 | 68,753 total registry contract-day rows (68,638 enumerated + 115 aggregated pre-arbiter file-day `UNKNOWN` rows) | Registry-vocabulary share over its own full row set |
| 41.89% (m5 report) | 28,754 (same numerator) | 68,638 exchange-enumerated official contract-days only | The m5 completeness denominator; identical numerator, narrower denominator |

The two missing-data figures therefore differ **only by denominator**: 28,754 ÷ 68,753 = 41.82%
vs 28,754 ÷ 68,638 = 41.89%. The registry reconciles against `logs/m5_completeness.json` on the
exchange-enumerated subset (`reconcile: MATCH`), so neither number was altered to force
agreement.

**Cost model audit (re-verified at HEAD).** `nifty_quant/engine/options_costs.py` charges on
premium turnover `|premium| × lot_size × lots`, entry and exit legs separately, partial exits
prorated. Side applicability matches the schedule: STT sell-only (0.05% to 2024-09-30, 0.1%
from 2024-10-01), stamp duty buy-only (0.003%), exchange transaction charge ₹20 per lot both
sides, SEBI 0.0001% both sides; GST 18% on brokerage + exchange + SEBI only (statutory levies
excluded from the GST base); slippage is an optional configured premium haircut (default 0).
Brokerage defaults to `model="none"` → ₹0, so no provider is assumed. `resolve_rate` raises
`UnknownChargeError` when no effective-dated regime covers a date (no silent cross-year
fallback). All 5 schedule rows carry `estimated=True` with provenance "not present in cached
NSE circulars" → `strict_verifiable=False` on every trade → the `transaction_costs_verified`
gate stays **closed**. No rate was invented or upgraded to verified.

**Four-way status distinction (binding for all future work).**

1. **Engineering implementation: complete.** Data layer, reference DB, coverage registry,
   candle datasets, cost/execution/accounting engines, readiness gate — all built and tested.
2. **Research modelling: permitted** within the cached archive under the stated exclusions
   (estimated costs, OI as estimate, coverage ceilings honoured).
3. **Strict financial validation: blocked** until official derivative-charge circulars flip
   `transaction_costs_verified` (0 of 5 rates sourced today).
4. **Strategy validation: not yet completed.** No options strategy has been run through the
   validation gate; the equity smoke run remains 0-of-6 passing by scope, not by quality.

---

*Evidence files:* `logs/p2_oi_availability.txt`, `logs/m2_volume_units.txt`,
`logs/m3_oi_norm.txt`, `logs/p2_reference.txt`, `logs/p2_coverage.txt`,
`logs/p2_candle_integrity.txt`, `logs/p2_options_cost.txt`, `logs/p2_execution.txt`,
`logs/p2_env.txt`, `logs/p2_readiness.txt`, `logs/pytest_p2.txt`;
tracked reference data under `data/reference/`; gate logic in `nifty_quant/validation/readiness.py`.

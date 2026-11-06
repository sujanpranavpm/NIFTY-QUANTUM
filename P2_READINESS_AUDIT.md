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

**Verdict: dedicated effective-dated engine on *premium turnover*; current-era rates sourced from official NSE circulars, the pre-2026 exchange rows estimated and excluded (fail-closed).**

- `nifty_quant/engine/options_costs.py` (`IndiaOptionsCommission`) computes brokerage (neutral
  default — **no broker assumed without configuration**; strict validation needs a *verified*
  broker config or explicit exclusion), STT on premium (sell side), the NSE exchange transaction
  charge **and the NSE IPFT contribution on premium turnover, each side** (P2.1 removed the
  fabricated `Rs 20 per lot` fee), SEBI, GST (18% on brokerage+exchange+IPFT+SEBI), stamp duty
  (buy leg) and slippage, on premium × lot_size × lots, **entry and exit separately**, partial
  exits prorated. Exports a per-trade breakdown mirroring the equity model (detail excludes `total`).
- The schedule `data/reference/options_charge_schedule.csv` is effective-dated and now carries
  `source_url` / `circular_ref` / `basis` / `verification` per row. The P2.1 audit
  (`logs/p2_options_cost.txt`, `scripts/_p2_cost_schedule_audit.py`) sourced the current-era rows from
  NSE circulars **FA73061** (Rs 3,552.99/cr txn + Rs 0.01/cr IPFT wef 2026-03-01) and **FA56129**
  (wef 2023-04-01), the **Finance (No.2) Bill 2024 cl.155** (STT 0.0625%→0.10% wef 2024-10-01) and
  the **NSE STT/levies/stamp** pages (STT 0.15% wef 2026-04-01; SEBI 0.0001%; stamp 0.003% buyer).
  The **P2.1.2** follow-up added the primary circulars **FA64232** (Sept-27-2024, the Oct-2024 uniform
  rate Rs 35.03/lakh = 0.03503% that FA73061 mis-cites as `64323`, a circular-reference mismatch,
  likely a typographical error) and
  **FA46730** (the 2021 slab schedule), lifting the schedule to **12 of 15 rows verified**. STT has
  three verified eras; exchange txn has two verified eras (2024-10-01 flat, 2026-03-01 flat) and the
  two **pre-Oct-2024 slab-era rows stay `estimated`** (values sourced, but the marginal slab a member
  pays is unknowable for a research book — a permanent convention, not a missing source); IPFT three
  verified eras. The pre-P2.1 schedule is preserved in `data/reference/options_charge_schedule_superseded.csv`.
- Worked round-trip recalculated (lot 75, 2 lots, premium 150→180; BUY turnover 22,500 / SELL
  27,000): BUY leg **Rs 10.1348**, SELL leg **Rs 38.3517**, **round-trip Rs 48.4865** — far below
  the old per-lot figure (Rs 122.13), which the fabricated fee had inflated. Since **P2.1.2** the
  **2025** trade is `strict_verifiable: True` (the Oct-2024 exchange era is sourced from FA64232,
  brokerage excluded) — it was `False` at the P2.1 snapshot when that era's date was only inferred;
  on **2026** dates the same trade is `strict_verifiable: True`. A **pre-Oct-2024 slab-era** trade
  (e.g. 2022) stays `strict_verifiable: False` (fail-closed on the estimated slab convention). FA73061
  kept txn+IPFT constant at Rs 3,553/cr, so the 2024-10 and 2026-03 eras cost the same. The equity
  `costs.py` is untouched (additive).
- Tests: `tests/test_options_costs.py` (component-by-component against a hand-computed trade, every
  historical STT/exchange/IPFT boundary, premium-turnover basis, entry/exit asymmetry, multiple lots,
  partial-exit proration, GST base + config fallback, verified/excluded/unverified brokerage, the
  recalculated worked example, reference-only exercised-option STT, unverified excluded).

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

- **516 passed, 0 failed, 0 skipped** is the authoritative count at HEAD after **P2.1.2**
  (`logs/_p212_full_suite.log`, re-run 2026-11-06). The history: P2 reconciliation **495**
  (`logs/pytest_p2.txt`) → P2.1 **+13** premium-turnover options-cost tests (**508**,
  `logs/_p21_full_suite.log`) → P2.1.2 **+8** new (6 in `test_options_costs`, 1 each in
  `test_options_accounting`/`test_options_execution`; nothing removed) = **516**. The **478** figure
  quoted at the Stage-7 commit was correct *at that commit*: Stage 8 then added the 17 readiness-gate
  tests to reach 495.
- Discovery parity from the repo root (`pytest`, `python -m pytest tests`) gives **zero skips**;
  running from inside `tests/` collects the identical suite but three disk-anchored tests skip
  because they read `data/derived`/`logs/` relative to the CWD — pre-existing, and why the
  repo-root invocation is the official one (the conftest pins the repo root onto `sys.path`).
- The long-`skipped` bracket case `test_bracket_stop_target_and_risk_reward` now runs on a
  deterministic `conftest` fixture (an engineered single golden cross), reaching **zero skips**.
  Building that fixture exposed and fixed a latent bug in `nifty_quant/signals/parsing.py`:
  `stop_price or limit_price` treated a pandas `NaN` as truthy and silently dropped every bracket
  take-profit (and stop price from the exit description). Type-selected `_order_price` now reads the
  correct field; the live bracket resolves the published **2:1 R/R** (`rr = 1.9967`).
- New Stage-7 modules cover the validation package, the reporting package, reproducibility and
  missing-data behaviour; the two Stage-6 option modules and the Stage-8 readiness gate add the rest.

## 12. Remaining data limitations (stated honestly)

- **Cost verification is the open gate.** P2.1 sourced the current-era charge-schedule rows and
  P2.1.2 added the primary circular FA64232 (**12 of 15** rows now verified; every rate in force from
  2024-10-01 sourced); the two **pre-Oct-2024 slab-era** `exchange_txn` rows stay `estimated` (values
  sourced but the marginal slab is an unobservable convention), so
  trades on those dates are **excluded from strict financial validation** (fail-closed), while a
  trade dated ≥ 2024-10-01 with brokerage excluded or verified is `strict_verifiable=True`.
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
- P2 ingestion is authorised only for non-strict research. P2.1 sourced the official
  derivative-charge circulars at ₹0 and P2.1.2 added the primary circular FA64232 (**12 of 15 rows
  verified**), but `transaction_costs_verified` stays open on the two pre-Oct-2024 slab-era
  `exchange_txn` rows. That is a **permanent, by-design exclusion**: the slab *values* are sourced
  (FA46730/FA56129) but the marginal slab a member pays depends on monthly turnover, which a research
  book does not have — no further document can close it, so the fail-closed per-trade exclusion
  (already enforced by the engine) is the correct terminal behaviour.
- **Two verification levels are now reported separately** (P2.1.2): `statutory_verified` (every
  statutory rate sourced, brokerage irrelevant) and `all_in_verified` (statutory **and** an attested
  broker fee). Excluding brokerage is *not* all-in verification — the fee is omitted, not verified —
  so a `model='none'` trade is `statutory_verified=True, all_in_verified=False` and its `cost_basis`
  refuses to be labelled a net figure. `strict_verifiable` (== statutory AND brokerage excluded-or-
  verified) is retained as the gate the accounting/execution engines still act on.

---

## 14. Reconciliation appendix (final consistency audit, 2026-11-05)

**Test count.** The authoritative count at HEAD is **508 passed, 0 failed, 0 skipped** after
P2.1 (`logs/_p21_full_suite.log`, re-run for this appendix); the P2 reconciliation was **495**
(`logs/pytest_p2.txt`) and P2.1 added 13 net options-cost tests (495 + 13 = 508). Documents
quoting 478 refer to the Stage-7 commit, before the 17 Stage-8 readiness tests existed; 478 + 17
= 495 and no test was ever deleted. Every report now cites 508. *(Superseded 2026-11-06 by the
P2.1.2 addendum at the end of this appendix: HEAD is now **516**.)*

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

**Cost model audit (re-verified at HEAD after P2.1).** *(Superseded 2026-11-06 by the P2.1.2
addendum: **12 of 15** rows verified, two slab-era rows estimated, current-era boundary now
2024-10-01.)* `nifty_quant/engine/options_costs.py`
charges on premium turnover `|premium| × lot_size × lots`, entry and exit legs separately, partial
exits prorated. Side applicability matches the schedule: STT sell-only (0.0625% to 2024-09-30,
0.10% from 2024-10-01, 0.15% from 2026-04-01), the NSE exchange transaction charge **and the NSE
IPFT contribution on premium turnover, each side** (P2.1 removed the fabricated `Rs 20 per lot`
fee), stamp duty buy-only (0.003%), SEBI 0.0001% both sides; GST 18% on brokerage + exchange +
IPFT + SEBI; slippage is an optional configured premium haircut (default 0). Brokerage is
configurable and defaults to `model="none"` → ₹0, so no provider is assumed; strict validation now
requires a *verified* broker config or explicit exclusion. `resolve_rate` raises
`UnknownChargeError` when no effective-dated regime covers a date (no silent cross-year
fallback). **11 of 15** schedule rows carry `verification="verified"` with an official
`source_url`; the three pre-2026 `exchange_txn` rows and the GST config knob stay `estimated`, so
`strict_verifiable=False` for trades on those dates and the schedule-level
`transaction_costs_verified` gate stays **closed** for the full horizon (while a current-era trade
≥ 2026-03-01 is strict-verifiable per trade). No rate was invented or upgraded to verified without
its official source; the pre-P2.1 schedule is preserved in
`data/reference/options_charge_schedule_superseded.csv`.

**Four-way status distinction (binding for all future work).**

1. **Engineering implementation: complete.** Data layer, reference DB, coverage registry,
   candle datasets, cost/execution/accounting engines, readiness gate — all built and tested.
2. **Research modelling: permitted** within the cached archive under the stated exclusions
   (estimated costs, OI as estimate, coverage ceilings honoured).
3. **Strict financial validation: blocked** — P2.1 sourced 11 of 15 rows and every current-era
   rate, but the three pre-2026 `exchange_txn` rows stay `estimated`, so
   `transaction_costs_verified` remains open for the full horizon (a current-era trade
   ≥ 2026-03-01 is strict-verifiable per trade).
4. **Strategy validation: not yet completed.** No options strategy has been run through the
   validation gate; the equity smoke run remains 0-of-6 passing by scope, not by quality.

### P2.1.2 addendum (2026-11-06) — supersedes the "11 of 15 / pre-2026 / 2026-03-01" figures above

The four coverage-denominator figures in the table are **unchanged and remain all correct** (they
are different denominators, not stale values). What P2.1.2 changed is the charge schedule and the
authoritative test count:

- **`NSE/FA/64232` sourced.** FA73061 cites the Sept-2024 uniform-rate revision as `NSE/FA/64323`,
  whose PDF is a 404; that is a circular-reference mismatch, likely a typographical error. The
  official primary document located is `NSE/FA/64232`, dated September 27, 2024, matching the subject
  and effective charges referenced by FA73061. It is now fetched
  and cached (`scripts/_p21_charge_sources.py`, verbatim text `logs/p21_excerpts_FA64232.txt`):
  *"Equity Options: Rs. 35.03 each side per lakh of premium value … with effect from October 1, 2024"*
  = 0.03503%. The **2024-10-01 `exchange_txn` row is `verified`**; the 2021 slab schedule was sourced
  from `NSE/FA/46730`. The schedule is therefore **12 of 15 rows verified** (was 11), and the strict
  per-trade boundary moves from **≥ 2026-03-01 to ≥ 2024-10-01**.
- **The two remaining estimated core rows are the pre-Oct-2024 slab eras, and they stay estimated
  permanently** — the slab values are sourced but the marginal slab a member pays is unobservable
  for a research book. `transaction_costs_verified` therefore stays **fail-closed for the full
  horizon** exactly as before; the four-way status (engineering complete / modelling permitted /
  strict validation blocked / strategy not validated) is **unchanged**.
- **`statutory_verified` and `all_in_verified` are now distinct** on `OptionsCostResult`; brokerage
  exclusion is `statutory_verified=True, all_in_verified=False` (never "net"). `strict_verifiable`
  is retained for the accounting/execution gates.
- **Suite: 516 passed, 0 failed, 0 skipped** from the repo root (`logs/_p212_full_suite.log`), +8 new
  tests over P2.1's 508, nothing removed. Regenerate the evidence with
  `python scripts/_p2_cost_schedule_audit.py` then `python scripts/_p2_readiness_checks.py`.

---

*Evidence files:* `logs/p2_oi_availability.txt`, `logs/m2_volume_units.txt`,
`logs/m3_oi_norm.txt`, `logs/p2_reference.txt`, `logs/p2_coverage.txt`,
`logs/p2_candle_integrity.txt`, `logs/p2_options_cost.txt`, `logs/p2_execution.txt`,
`logs/p2_env.txt`, `logs/p2_readiness.txt`, `logs/pytest_p2.txt`;
tracked reference data under `data/reference/`; gate logic in `nifty_quant/validation/readiness.py`.

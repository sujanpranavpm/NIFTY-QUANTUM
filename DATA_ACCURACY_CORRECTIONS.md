# DATA_ACCURACY_CORRECTIONS.md — data-integrity corrections + T9

Deliverable of the milestone that followed P1. P0 and P1 are committed (`2f39c23`, `fab1482`);
everything here is the work done after that commit, and nothing beyond it is authorised.

Every number below was produced by a named script under `scripts/` and lives in the named
log under `logs/`. `UNVERIFIED` marks what the ₹0 resource envelope cannot settle. Nothing
was filled in with a plausible value, and no raw provider value was overwritten.

Scope actually executed: volume-unit verification across the whole free official archive,
the January-2025 open-interest scale investigation, the terminal-candle convention
decision, an expiry/day/contract completeness report, a normalisation layer in
`nifty_quant/derivatives/`, the T9 core test suite, pytest discovery, dependency hygiene.
**Not executed, by instruction: full data ingestion, strategy optimisation, dashboard,
live trading, and 5-minute/15-minute candle production outside strict-validated blocks.**

---

## 1. Volume-unit verification

**Verdict: the Hugging Face `volume` column is a share-equivalent — contracts × that
contract's board lot on that day. Conversion to contracts therefore divides by the lot of
the same contract row, never by a constant and never by a rupee figure.**
Evidence: `scripts/_m2_volume_units.py` → `logs/m2_volume_units.txt` / `.json`;
`scripts/_m2_lot_schedule.py` → `logs/lot_schedule.txt`;
`scripts/_m2_diag{,2,3}.py` for the residual anomalies.

The relationship was verified **before** any conversion was shipped, over
**310 file-days spanning 263 distinct official dates** (2024-01-01 → 2026-07-02) — every
date the free NSE archive serves, not a sample. Each official report row supplies both sides
of the equation: `TtlTradgVol` (contracts) and `NewBrdLotQty` (that contract's lot).

Grouped by the lot size(s) the exchange reported on that date — the "historical regimes"
the milestone required covering:

| official lot(s) | file-days | dates | contracts | median exact | median row ratio | median non-integral bars | sum &#124;delta&#124; |
|---|---|---|---|---|---|---|---|
| `(65,)` | 13 | 2026-02-16 .. 2026-07-02 | 1,477 | **100.00%** | 1.00000 | 0.000% | **0 shares** |
| `(65, 75)` | 7 | 2025-11-17 .. 2025-11-25 | 1,303 | **100.00%** | 1.00000 | 0.000% | **0 shares** |
| `(75,)` | 92 | 2025-01-31 .. 2025-10-28 | 5,603 | **100.00%** | 1.00000 | 0.000% | 724,482,825 shares |
| `(25, 75)` | 68 | 2024-11-22 .. 2025-01-30 | 9,058 | 0.00% | 1.00000 | 0.368% | 5,602,135 shares |
| `(50,)` | 27 | 2024-01-01 .. 2024-04-25 | 3,633 | 0.00% | 1.00002 | 0.498% | 1,090,162 shares |
| `(25,)` | 103 | 2024-05-21 .. 2024-11-21 | 5,219 | 0.00% | 1.00036 | 1.583% | 1,044,015 shares |

Reading the table honestly, which is the point of the milestone:

- **Current regime (lot 65, and the 65/75 transition) matches to the share.** Summed
  absolute difference exactly zero across 20 file-days and 2,780 contracts. This is the
  strongest evidence in the file and it is on the data a strategy would trade today.
- **The 724,482,825-share residual is not a unit error.** One file-day — `2025-09-26` ×
  expiry `2025-09-30` — supplies essentially all of it, and that session stops at 15:00
  (346 of 376 minute labels, 9.7% of the day's volume absent). It is a **truncated
  download**, now classified as such (§5), not a counter-example to the unit.
- **"median exact 0.00%" on the 2024 rows is the `lot·k + 1` artifact, not disagreement.**
  The *median row ratio* is 1.00000–1.00036 — day totals agree to within four hundredths of
  a percent — but the per-file-day count of contracts that match with *absolute exactness*
  is dragged to 0% because a handful of minute bars sit one share above a whole multiple of
  the lot. Those bars are 0.37–1.58% of minute bars and are concentrated at the 09:15
  opening bucket (`scripts/_m2_diag2.py`), where the auction print is not a whole-lot trade.
- **`TtlTrfVal` was tested as an arbiter and rejected.** Median ratio to the archive's own
  rupee value is 0.0016: for options it is a notional/strike-based figure. Rupee turnover
  must never be used to decide a unit (recorded in `DATA_SOURCE_REGISTER.md` §2).
- **Direction of the residual.** 726 contract-days sit above official × lot beyond 1%, 169
  below. Both tails are dominated by low-volume strikes where one opening bucket moves the
  day total by a whole percent; neither is a unit error. The archive is not systematically
  high or low.

**What is preserved.** `normalise_units()` adds columns and touches nothing raw:
`volume_share_equivalent` (the raw provider number, re-labelled with its unit),
`volume_contracts` (the conversion), `volume_integral` (whether every bar was a whole
multiple), `lot_size`, `lot_basis` and `lot_estimated` (how that lot was known),
`units_verified` (whether an official arbiter exists for that date — false before
2024-01-02), and `unit_provenance` (the sentence that explains the conversion). A second
normalisation pass over its own output reproduces the same numbers, which is pinned by a
test.

**Historical lot sizes** (modal lot per official date, `logs/lot_schedule.txt`): 75 up to
2021-11-30 *(no official arbiter — inferred from the archive's smallest positive volume,
`estimated=True`)*, 50 from 2021-12-01, 25 from 2024-04-30, 75 from 2024-12-20, 65 from
2025-12-24 (current as of 2026-11-03). **61 of the 318 cached dates carry two lots at
once** (25+75 from 2024-11-22 to 2025-01-30, 75+65 from 2025-10-30 to 2026-01-02), because
a revised lot applies only to newly issued expiries. A date-only lot lookup is therefore a
**fallback** and is returned with `estimated=True`; the authoritative value is the
per-contract-row `NewBrdLotQty`, and the official row wins whenever both are available
(also pinned by a test).

## 2. Open-interest correction validation

**Verdict: provider `open_interest` is a share-equivalent through 2024-12-31 and one
hundredth of a share-equivalent from 2025-01-01 onward. The date-aware ×100 correction
raises agreement with the official end-of-day figure from 0.00% to 73.60% on the post-break
side, and every bar it touches is labelled `estimated`.**
Evidence: `scripts/_m3_oi_norm.py` → `logs/m3_oi_norm.txt` / `.json`.

- **Scale change located and confirmed on 262 official dates.** Last pre-break date
  `2024-12-31`: median raw ratio 0.997. First post-break date `2025-01-01`: median raw ratio
  100.003. **Dates whose ratio contradicts their side of the break: none.** The break has
  the signature of a pipeline change on a calendar boundary, not market behaviour, and it is
  the same date as the intraday-range defect P1 found.
- **Conversion validation, by side of the break:**

  | side | file-days | comparable contracts | within 1% raw | within 1% converted | `estimated` flag |
  |---|---|---|---|---|---|
  | provider = share-equivalent (pre-break) | 167 | 13,120 | 8,366 (63.77%) | 8,366 (63.77%) — identity | `[False]` |
  | provider = 1/100 (post-break) | 142 | 8,105 | **0 (0.00%)** | **5,965 (73.60%)** | `[True]` |

  Pre-break dates wrongly flagged estimated: none. Post-break dates wrongly presented as
  measured: none.
- **Independent-verification ceiling, stated as a limit rather than a capability.** Of
  21,225 comparable contract-days, **14,331 (67.52%)** have a last-bar converted OI within 1%
  of the official EOD figure. That is the ceiling for OI-based intraday features; the
  remaining 6,894 stay unverified and must not feed a signal. Before the arbiter floor of
  2024-01-02 no independent verification exists at ₹0, so conversions there are unverified
  **by definition** (`units_verified=False`).
- **The EOD figure is not an intraday maximum.** Intraday *peak* OI matches the official EOD
  value on only 3,863/21,225 (18.20%) contract-days. Anyone building a "peak OI" feature from
  the archive is measuring something the exchange never reported.
- **Missing versus zero, counted.** Across 5,738,797 bars in compared file-days: provider OI
  == 0 on **2,029,593 (35.37%)**, NaN on **0**, negative on **0**. 5,027 contract-days carry
  provider OI == 0 while official OI > 0 — a *missing observation*, never filled — and only
  34 contracts genuinely had no positions to report. Zero is treated as absence of an
  observation, not as a closed position.
- **No manufacturing, no leakage.** On a 34,617-bar sample block (`2024-01-15` ×
  `2024-01-25`): the raw column is unaltered in 34,617/34,617 bars, and raw zeros stayed
  converted zeros — a fill would have changed that count. The official figure is attached
  only on bars at or after 15:30 IST: 29 bars (0.08%) carry it, earliest clock
  `2024-01-15 15:30:00+05:30`, **0 pre-close bars**. A deliberately leaked copy (106 bars
  back-filled to 09:15) is caught:
  `LookAheadError: 106 bars carry oi_official_eod before 15:30:00`.
  `attach_official_eod()` writes `oi_official_eod_visible_from` and
  `oi_official_eod_source` on every row, and `assert_no_eod_leakage()` re-checks any frame.

`DATA_SOURCE_REGISTER.md` §2 also records a correction to *this project's own* earlier
claim: official `OpnIntrst` is a **share-equivalent**, not a contract count. That stale row
is what made the archive look 100× wrong on one side of the break only.

## 3. Terminal-candle decision

**Decision: the `timestamp` column is the OPEN of the interval — a bar stamped `T` covers
`[T, T+1)`. `15:30` is not a candle: it is the closing/settlement print. This is now the
single convention in `nifty_quant/derivatives/timestamps.py` (`CONVENTION =
"open_labelled"`), and aggregation raises rather than guesses if the convention is
unresolved.**
Evidence: `scripts/_m4_terminal_candle.py` → `logs/m4_terminal_candle.txt`;
`scripts/_m7_check.py` → `logs/_m7_check.log` (68 date × expiry blocks).

The four tests the milestone asked for, in the order they were run:

1. **Source documentation.** The dataset card (1,009 chars, fetched anonymously) names the
   columns and says "1-minute OHLCV(+OI) bars" with a "tz-aware IST" `timestamp`. It does
   **not** state open or close labelling. So documentation alone cannot settle this, and no
   claim was made from it.
2. **Session boundaries.** 39 of the 68 analysed blocks present the signature
   `09:15 … 15:30` with 376 distinct labels; both endpoints are inside the exchange's
   session, so an extra closing bucket would have to be invented to make the labels
   close-time. The label set alone is ambiguous — which is exactly why P1 left W3 open.
3. **Volume profile of the two endpoints** (the decisive test). Under open-labelling the
   09:15 bucket holds the opening auction and the 15:30 bucket must hold one settlement
   print:
   `STRICT_OK` blocks show an **opening share of 1.241–14.582× a typical minute
   (median 4.086×)** and a **terminal share of 0.0002–0.4286× (median 0.0134×)**.
   The 15:30 print is 0.038× a typical minute and **83.3% single-priced**
   (open == high == low == close). The two distributions are separated by more than an
   order of magnitude at the 1.0 / 0.5 thresholds; close-labelling cannot reproduce that
   shape, because it would put the auction in the 09:16 bucket and leave 09:15 empty.
4. **NSE `LastPric`.** The 15:30 bar's close equals the official `LastPric` for **100% of
   matched contracts**. `ClsPric` was not used (P1 showed it agrees with the daily close on
   only 3.89–43.04% of contracts) and `SttlmPric` is a derived settlement value.

**Ambiguity is rejected, not guessed.** `classify_session` verdicts across the 94 blocks
checked by `_m7_check.py`:

| verdict | blocks |
|---|---|
| `STRICT_OK` | 60 |
| `AMBIGUOUS_OUT_OF_SESSION` | 12 |
| `AMBIGUOUS_OPENING_BUCKET` | 10 |
| `NO_CLOSING_PRINT` | 5 |
| `AMBIGUOUS_TERMINAL_BUCKET` | 3 |
| `AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET` | 2 |

The 12 out-of-session blocks carry stray labels up to **15:54** (`2025-01-02` 15:46,
`2025-01-06` 15:54, `2025-08-01` 15:41, and 9 more, all January–March 2025 or 2025-08-01);
`_m7_check.py` shows one rejected with `1 labels outside 09:15..15:30: ['15:45']; no
convention can be verified for them`. Those sessions are excluded from strict validation
rather than assigned a convention.

**Aggregation, now implemented but not run for production.** A session is 375 in-session
minute buckets (`09:15 … 15:29`); `15:30` is a closing print folded into the final bucket
via `CLOSING_PRINT_INDEX = 375` clamped to `LAST_BUCKET_INDEX = 374`. 5-minute
aggregation therefore yields **75 candles** (last label 15:25) and 15-minute **25 candles**
(last label 15:15). Grids that do not tile 375 are refused (1 works; 2, 6 and 7 raise).
Verified on real blocks: `2025-11-25` 5m rows=10,860, max candles/contract=75, final candle
15:25 for 74.6% of contracts with the closing print inside it for 12.2%, share-volume
integral on 100.00% of rows, OI unit `one_hundredth_share_equivalent` with
`estimated=[True]`; `2024-01-18` 5m rows=12,031, max 75, final candle 15:25 for 83.8%,
integral 99.52%, OI `share_equivalent` with `estimated=[False]`. **No 5m/15m dataset has
been produced** — that is P2 work, and the milestone forbids it until this was settled.

## 4. Historical reference-data verification

Evidence: `scripts/_m2_lot_schedule.py` → `logs/lot_schedule.txt`;
`scripts/_p1_reference_data.py`, `scripts/_p1_nse_circulars.py` →
`logs/p1_nse_circulars_text.txt`; `scripts/_p1_expiry_calendar.py` →
`logs/p1_expiry_calendar.txt/.json`; registry in `nifty_quant/derivatives/reference.py`.

- **Lot sizes.** `NewBrdLotQty` is read per contract row from all 318 cached official
  reports — the only historical lot arbiter this project trusts. The circulars explain cadence
  and tick rules but publish no lot time series, so the earlier habit of citing a circular for
  a lot was dropped. Five regimes, contiguous and distinct: 75 (pre-2021-11-30, **inferred, no
  arbiter**), 50 (2021-12-01..2024-04-29), 25 (2024-04-30..2024-12-19), 75
  (2024-12-20..2025-12-23), 65 (2025-12-24..present).
- **The pre-arbiter inference is visible in the data.** `2021-05-27` shows min positive
  volume 75 with smallest distinct volumes `[75, 76, 150, 151, 152, 225]`, and
  `2021-12-30`, `2022-11-24`, `2023-06-22`, `2023-12-28` show 50 with
  `[50, 51, 100, 101, 102, 150]` — each the lot its own regime was later *confirmed* at on the
  official side of the break, which is why the inference is credible and still labelled
  `estimated`. On `2022-09-29` the minimum is `1`, so the **modal** lot is used there, never
  the minimum.
- **Expiry mapping.** Expiry identity is normalised through one function,
  `expiry_from_iso()`, which accepts `date`, `datetime`, `pandas.Timestamp`, ISO strings and
  the exchange's `26-Dec-2024` style, and always returns an ISO *date*. This closed a real
  defect: `pandas.Timestamp` is a subclass of `datetime`, so an aware timestamp was
  previously accepted as an "ISO date", and a bhavcopy-style `26-Dec-2024` row could never
  join to a provider-style `2024-12-26`. A test now proves one official row is reachable from
  five provider spellings, and that an unparsable expiry raises rather than guesses.
- **Expiry cadence.** Verified against primary circulars, and the weekday is *not* constant:
  NIFTY weekly and monthly expiries moved to **Tuesday from 2025-09-01** (`NSE/FAOP/68747`),
  which invalidates any "last Thursday of the month" rule for the second half of the
  dataset. `FAOP/54334` (weekly count 7→4 from 2022-12-09) and `FAOP/64506` (one benchmark
  index per exchange, weeklies from 2024-11-20) are the other two verified regime changes.
- **Tick size.** ₹0.05 for NIFTY *option premiums*, adopted against the contract-specs slab
  that would put it at ₹0.10 above index 15,000: official traded prices are 0.00% off the
  ₹0.05 grid on every audited date through 2026-08-10, the archive is 100.00% on-grid, and
  Breeze's own master carries `TickSize = 5` on 3,388 of 3,391 NIFTY rows. Two independent
  observations beat rule text; the contradiction stays recorded in
  `DATA_SOURCE_REGISTER.md` §3 and is not re-litigated. From 2026-08-03 the *derived*
  columns (`PrvsClsgPric`, `SttlmPric`) go 20.14–23.43% off-grid while traded columns stay
  on it — an NSE settlement-computation change, not a tick change.

## 5. Missing-data assessment

Evidence: `scripts/_m5_completeness.py` → `logs/m5_completeness.txt` / `.json`. Vocabulary:
`nifty_quant/derivatives/completeness.py`. The exchange universe is the 318 cached official
reports; the provider inventory is the P1 snapshot (`logs/p1_hf_coverage.json`, 267 NIFTY
expiry files); the audited payload is the 23 locally cached NIFTY expiry files (98 MB).

**Expiry level — four causes, kept apart:**

| status | count | meaning |
|---|---|---|
| `PRESENT` | 17 | cached here and complete up to its expiry day |
| `TRUNCATED_TAIL` | 1 | `2026-08-04` — cached but stops on 2026-07-02 |
| `PUBLISHED_NOT_CACHED` | 115 | the provider publishes it, this audit never downloaded it — an **audit gap**, never a provider defect |
| `MISSING_FILE` | 46 | exchange listed it, provider publishes no file |
| `PUBLISHED_NO_OFFICIAL_EVIDENCE` | 135 | provider publishes it, the cached official reports never list it (mostly 2021-2023, before the arbiter floor) |

The 46 `MISSING_FILE` are **not** 46 holes. Split against the archive's own last trading day
(`2026-07-02`, derived from the data rather than asserted):

- **9 real holes** — expiries the exchange listed, that traded inside the archive's span, and
  for which the provider publishes nothing: `2025-09-25`, `2025-10-20`, `2025-12-24`,
  `2026-03-26`, `2026-03-31`, `2026-06-16`, `2026-06-23`, `2026-06-25`, `2026-06-30`.
  Three of them (`2025-12-24`, `2026-03-31`, `2026-06-25`) were on the listed-contract
  universe of 217 official dates, so these are substantial monthlies, not rounding.
- **37 beyond the horizon** — `2026-08-11` onward, incl. 2027/2028/2029/2030/2031 expiries the
  exchange lists years ahead. The archive stops on 2026-07-02, so those contracts have had no
  session to publish; calling them "missing" would overstate the defect.
- 2021-2023 expiries cannot appear in either list: the free official archive begins
  2024-01-02, so their absence is **unverifiable, not proven**.

**The incomplete August 2026 file, resolved.** `options__NIFTY__2026-08-04.parquet` is
58,248 bytes locally and the provider's published size is **58,248 bytes** — the download is
provably complete, so the short file is the **provider's**, not this audit's. It holds 2,646
rows over 6 days (`2026-06-24 .. 2026-07-02`) where the exchange listed this expiry on 15
official dates; it carries neither its expiry session nor the sessions between 2026-07-02 and
2026-08-04. Verdict `TRUNCATED_TAIL`, 9 missing days, and the 9 file-days the provider left
out entirely for this expiry are itemised (2026-07-03 … 2026-08-04, 47–182 contracts traded
per the exchange on each).

**Contract-day level — 68,638 official contract-rows in countable file-days:**

| verdict | cells | share | what it is |
|---|---|---|---|
| `MISSING_DAYS` | 28,754 | 41.89% | exchange reports traded volume, provider has no minute bar for that contract that day |
| `PRESENT` | 21,168 | 30.84% | fully observed |
| `NO_TRADES` | 13,591 | 19.80% | official volume zero — absence of bars is **correct**, not a gap |
| `PRESENT_WITHOUT_OI` | 4,786 | 6.97% | bars present, no open-interest observation |
| `TRUNCATED_SESSION` | 338 | 0.49% | inside a file-day that stops before the close |
| `MISSING_TRADES` | 1 | 0.00% | whole session, day total below the 99% floor (`2024-01-23 × 2024-01-25 22500 PE`, 91.74%) |
| *unverifiable* | 115 file-days | — | no official arbiter (2021-2023), reported as such |

Where the 41.89% actually sits — this is the finding P2 has to design around, so it is broken
down rather than averaged away:

- By exchange-reported liquidity: **20,565 cells had more than 100 contracts traded**, 4,531
  had 11-100, and only 3,658 had ≤10. **This is not "far strikes nobody trades"** — it is the
  provider's chain snapshot being partial, exactly as its own card warns
  ("illiquid/far strikes are often sparse or absent"), but the card understates how far into
  the liquid strikes that reaches.
- It is **spread, not concentrated**: the worst single covered file-day contributes 260 cells
  (`2024-12-13 × 2024-12-26`), then 237, 236, 232, 220 … Only 9 file-days are absent entirely,
  and all 9 belong to the one truncated `2026-08-04` file. So no single "hole day" explains
  the count, and no re-download will fix it.
- Consequence, stated as a rule for P2: a backtest universe must be built from the contracts
  the **archive actually holds**, and any contract-day with official volume and no bars must be
  excluded from price-based statistics rather than zero-filled. Reporting coverage as
  "100% of listed contracts" would be false.

**Truncated sessions, all three.** `2025-09-26 × 2025-09-30` (last label 15:00, 43,619 bars),
`2025-10-21 × 2025-10-28` (14:45, 7,869 bars), `2021-05-14 × 2021-05-27` (09:15, 92 bars — most
likely the expiry's first listing day, but classified conservatively as truncated so strict
validation drops it either way). These are the four-way distinction the milestone asked for:
**missing data** (`MISSING_FILE`, `MISSING_DAYS`) vs **inactive contracts** (`INACTIVE`:
unlisted, or expiry not yet reached) vs **missing trades** (`NO_TRADES`, `MISSING_TRADES`) vs
**truncated downloads** (`TRUNCATED_TAIL`, `TRUNCATED_SESSION`) — and the byte-size check that
separates a provider short file from our own failed download.

Nothing is silently skipped: every non-`PRESENT` verdict carries a reason string and an
example, and the JSON carries the itemised lists.

## 6. Expanded NSE reconciliation

P1 reconciled **21** dates. This milestone reconciles the **entire free official archive —
318 cached bhavcopies covering 2024-01-02 → 2026-11-03, 0 dates failed to parse** — at ₹0 cost,
because the bytes were already cached under `data/raw/p1_nse/` and no new download was needed.

| reconciliation | official dates | file-days | what it settled |
|---|---|---|---|
| volume units (`_m2_volume_units.py`) | **263** | 310 | provider volume = share-equivalents; exact on 20 of the 27 current/mixed-lot file-days |
| open interest (`_m3_oi_norm.py`) | **262** | 309 | the 2025-01-01 scale break, zero contradicting dates; ×100 branch validated 0.00% → 73.60% |
| completeness (`_m5_completeness.py`) | **318** | 68,638 contract-rows | expiry universe (178 listed expiries), 9 provider holes, the four-way gap vocabulary |
| lot schedule (`_m2_lot_schedule.py`) | **318** | — | five lot regimes, 61 dates with two lots live at once |
| terminal candle (`_m4_terminal_candle.py`, `_m7_check.py`) | 68 blocks (94 checked) | — | open-labelled convention; 60 blocks strict-OK, 28 rejected with reasons |

Historical regimes covered, which is what "across representative contract-days and historical
regimes" requires — no regime in the audited span is untested: lot-50 (2024-01..04), lot-25
(2024-05..11), the 25+75 overlap (2024-11..2025-01), lot-75 (2025-01..10), the 75+65 overlap
(2025-10..2026-01), lot-65 (2026-02..07), and the pre-arbiter 2021-2023 era, which is
**single-source by construction** and is reported with `units_verified=False` instead of being
silently converted. Beyond 2024-01-02 there is no cheaper arbiter at ₹0, so no further
widening is possible; the archive itself stops at 2026-07-02, and 2026-08 onward can only be
verified once the provider publishes it.

## 7. T9 test results

**`300 passed, 1 skipped`** (`pytest` / `python -m pytest tests` from the root; also
`python -m pytest` from inside `tests/`; also bare `pytest -q` from the root — all four
invocations verified identical). The single skip is the pre-existing
`test_bracket_stop_target_and_risk_reward`: `orders_12` produced no fresh BUY at the end of
the 3-symbol smoke sample, so the bracket R/R assertion has nothing to check. Nothing was
weakened to achieve green.

| file | tests | status |
|---|---|---|
| `tests/test_timestamps_candles.py` | 73 | new (this phase) |
| `tests/test_lot_reference.py` | 39 | new (this phase) |
| `tests/test_completeness.py` | 38 | new (this phase) |
| `tests/test_position_accounting.py` | 31 | new (this phase) |
| `tests/test_data_units.py` | 28 | new (this phase) |
| `tests/test_lookahead_guards.py` | 22 | new (this phase) |
| `tests/test_fill_engine.py` | 40 | new earlier in this milestone |
| `tests/test_costs.py` | 19 | new earlier in this milestone |
| `tests/test_signals.py` | 11 (1 skipped) | the T8 baseline, re-verified unchanged |

The P1 baseline commit carried **only** `tests/conftest.py` and `tests/test_signals.py`
(`git ls-tree --name-only HEAD tests/`), so **290 of the 300 passing tests are new in this
milestone**: 231 added while closing the data-accuracy items, 59 covering the cost model and
the fill engine from the earlier part of the same milestone, and the 11 T8 signal tests.
All of the new tests are **deterministic fixtures from `tests/conftest.py`**
(`minute_block`, `official_eod`, `minute_labels`, `DEFAULT_DAY`, `DEFAULT_EXPIRY`): no network,
no `data/`, no `artifacts/`, and **no dependence on the smoke dataset** — which is the
milestone's explicit requirement for testing core execution logic.

Coverage against the 14 areas the milestone listed:

| area | where |
|---|---|
| volume conversion | `test_data_units.py` — 7500/75 = 100 exact; 76 → residual +1, 74 → −1, `integral=False`; `lot<=0` raises on both dividers; idempotence of a second pass |
| OI conversion | `test_data_units.py` — pre-break identity, post-break ×100 with `estimated=True`, per-row `oi_unit`, unknown lot → `["unknown"]` + NaN, NaN volume tolerated, zero OI stays zero |
| historical lot-size mapping | `test_lot_reference.py` — 14 parametrised regime-boundary days, contiguity/distinctness, every date-only answer `estimated`, per-regime provenance vs the arbiter floor, mixed-lot dates, official-row-beats-date-regime |
| expiry mapping | `test_lot_reference.py` — `expiry_from_iso` × 8 forms + 3 unparsable raising, `26-Dec-2024` reachable from 5 provider spellings, `contract_key`, incomplete identity → fallback |
| timestamp handling | `test_timestamps_candles.py` — half-open `[T, T+1)`, boundary membership, naive/UTC/IST mixed awareness, `session_close_stamp`, `minute_index`/`candle_index`/`candle_label` grid maths including the 15:30 fold |
| candle aggregation | `test_timestamps_candles.py` — hand-built OHLC/volume/last-OI exactness, clock-order independence, closing-print fold, no empty-interval filling, NaN OI preserved, volume conservation, 5m/15m agreement, non-tiling grids refused, unresolved-convention raises |
| transaction costs | `test_costs.py` — total commission, per-component breakdown, `fees_detail` excluding "total" |
| brokerage and statutory charges | `test_costs.py` — brokerage 0.0003 capped ₹20 (crossover 66,666.67), STT 0.001/0.00025, exchange 0.0000297, SEBI 0.000001, stamp 0.00015/0.00003, GST 18% on brokerage+exchange+SEBI only, DP ₹13.5 sell-delivery only |
| slippage | `test_fill_engine.py` + `test_position_accounting.py` — slippage tracked separately from commission, never touches position or realized P&L |
| order fills | `test_fill_engine.py` — market at `bar.open`, `_theoretical_price` for limit/stop in both directions, ORDER_PRIORITY ordering, OCO/bracket/trailing/stop-limit |
| stop-loss and target execution | `test_fill_engine.py` — bracket decomposition, stop fills, `self.cancel(parent)` semantics pinned (verified correct, not a bug) |
| position accounting | `test_position_accounting.py` — weighted average entry, partial close, last-shares-out clearing, flip through zero, short-covering behaviour pinned as a documented limitation, the auditable fill ledger |
| equity calculations | `test_position_accounting.py` — `market_value`/`equity` with unusable prices skipped, round-trip equity = capital − costs, `gross_exposure` both sides, cash-guard buffer `1.005` sized against the worst-case statutory stack |
| look-ahead prevention | `test_lookahead_guards.py` — official EOD visible only at/after 15:30 IST, `session_close` tightening, a deliberate `.bfill()` leak raises `LookAheadError`, only named columns checked, provider OI never replaced, raw/detached columns kept separate |

Writing the tests found **five real defects in code I had written earlier in the milestone**,
each now fixed and covered: `expiry_from_iso` and `completeness._as_iso` accepted a
`pandas.Timestamp` as an "ISO date" (subclass of `datetime`); `LotTable.lot_for` and
`units._normalised_keys` looked up keys in a different normalised form from the one that
stored them, so a cross-format expiry could never join; and `classify_day` counted a
zero-trade contract-day that still carried open interest as `MISSING_DAYS`, which would have
inflated §5's headline count. A sixth fix was in the M5 script itself: three frames keyed
`(day, strike, option_type)` were merged on `(strike, option_type)` only, so each official row
fanned out across every day in the file — contract-days were judged on another day's bars and
the cell count grew with days². That run took **over six hours and never finished**; after the
fix the same report completes in **~8 minutes** and the numbers above are the corrected ones.

## 7b. Test discovery and dependency hygiene (items 6 and 7)

**Item 6 — `pytest.ini`, no vendor edits.** The official command is now simply `pytest` (or
`python -m pytest tests`) from the root, and `python -m pytest` from inside `tests/`; all
verified identical. Before it, a bare `pytest` from the root also collected
`vendor/quantjourney-bt/tests/`, which imports a package (`backtester`) that is not installed
here, and the run aborted with **8 `ModuleNotFoundError` collection errors** before any
project test ran — a real false-red at the P1 close-out. It is fixed by configuration only:
`testpaths = tests` plus `norecursedirs = vendor .* build dist node_modules __pycache__ data
artifacts logs reports scripts`. **Not one byte under `vendor/` was modified**, and none
should be to make collection look clean.

**Item 7 — audit-only dependencies split out.** `curl_cffi>=0.7` and `pypdf>=5.0` were
removed from `requirements.txt` and now live in `requirements-audit.txt`, with the honesty
check recorded there: nothing under `nifty_quant/` imports either (grep-verified), so they are
not production dependencies — they exist only so a reviewer can re-run the NSE probes and
circular-PDF extraction. **No existing package was upgraded or re-pinned.** One drift is
recorded rather than silently fixed, because changing it is your call: `requirements.txt`
pins `pandas>=2.2,<3.0` while the environment this repo was validated in runs **pandas
3.0.1** and the suite passes on it — a fresh `pip install -r requirements.txt` would get 2.x.
`polars`, `duckdb` and `fastparquet` remain uninstalled; no code assumes them.

## 8. Updated limitations

1. **2021-05 → 2023-12 is single-source.** The free official archive begins 2024-01-02, so
   volume, OI and lot conversions there carry `units_verified=False` and the lot is **inferred
   from the archive's own volumes** (`estimated=True`). No ₹0 path exists to change this.
2. **Provider OI is a snapshot that changes scale on 2025-01-01**; the correction is an
   **estimate** and only 67.52% of comparable contract-days can be independently verified
   (14,331 of 21,225). 35.37% of bars carry OI == 0 and are never filled. Peak intraday OI is
   not the EOD OI (18.20% match).
3. **The archive's contract coverage is partial**: 41.89% of official contract-rows in
   countable file-days have no provider minute bar, 20,565 of them on contracts that traded
   more than 100 contracts. Any universe definition must be derived from the archive, not from
   the exchange's listing.
4. **9 provider holes** (listed expiries inside the archive's span with no file), **1
   truncated expiry file** (`2026-08-04`), **3 truncated file-days**, and **115 expiries
   published but not downloaded here** (audit gap, not provider defect).
5. **28 of 94 audited blocks cannot be given a verified candle convention** and are excluded
   from strict aggregation: 12 carry labels past 15:30 (up to 15:54), 10 have an untrusted
   opening bucket, 5 have no closing print, 3 an ambiguous terminal bucket, 2 no trusted first
   bucket.
6. **No depth, bid/ask, per-minute trade counts, or greeks** in any permitted source;
   `TtlTrfVal` is notional-based and cannot supply rupee turnover for options either.
7. **No options cost model.** `nifty_quant/engine/costs.py` is an equity delivery-cost model;
   per-lot derivative charges, and hence any options P&L number, are not trustworthy yet.
8. **Realized P&L in `accounting.py` is long-side only** — a short closed at a profit credits
   nothing. Now pinned by an explicit test and documented, not hidden.
9. **Hugging Face licence is `cc-by-nc-4.0`**: non-commercial research only, no redistribution,
   attribution required for anything derived. Cloud Trader Pro (CC0 *as declared*) was never
   retrieved — a Google sign-in wall, zero bytes — and Breeze needs an account, so neither is
   claimed usable.
10. **`pandas` version drift**: `requirements.txt` pins `>=2.2,<3.0` while 3.0.1 is installed
     and the suite passes on it. Recorded, deliberately not re-pinned mid-milestone.
11. **`nifty_quant/validation/` statistics and `nifty_quant/reporting/` plots remain untested**
     — the part of T9 the milestone's 14 areas did not require.

## 9. P2 readiness assessment

**Ready: yes for a *breadth* ingestion through the layer that now exists; no for strategy
signals that depend on open interest.**

- The ingestion hazards that would have made P2 silently wrong are each now closed and tested:
  unit conversion with provenance, per-row historical lots with an `estimated` date fallback,
  cross-format expiry identity, date-aware OI rescale, the 15:30 EOD visibility gate with a
  re-checkable guard, the open-labelled convention, and a gap vocabulary that forces the four
  causes apart.
- Raw files are never modified; every derived value carries unit, basis, provenance and an
  `estimated` flag, so a P2 table can be re-derived and audited without re-deriving the
  science.
- **What P2 must not do without a new decision:** use OI as an intraday feature on
  unverified contract-days (cap 67.52%); define the tradable universe from the exchange
  listing rather than the archive's 58%-of-rows coverage; aggregate any block that is not
  `STRICT_OK`; trust a date-only lot where the official row is available; or assume a rupee
  turnover figure exists for options.
- **Blocking work that is not data:** an options cost model (item 7 above) is required before
  any options backtest number is meaningful, and it is out of this milestone's scope.
- Verification floor moves only if the bytes arrive: re-checking 2026-08 onward requires the
  provider to publish it, and pre-2024 requires a paid source. Neither is available at ₹0.
- **Authorisation state: P2 has not started.** No full-universe ingestion, no strategy
  optimisation, no dashboard, no broker/live trading — per instruction. The gate agreed at P1
  remains: these reports get reviewed first.

---

### Files this milestone added

`nifty_quant/derivatives/` — `reference.py`, `units.py`, `timestamps.py`, `candles.py`,
`completeness.py` · `pytest.ini` · `requirements-audit.txt` ·
`scripts/_m2_volume_units.py`, `_m2_lot_schedule.py`, `_m2_diag{,2,3}.py`, `_m3_oi_norm.py`,
`_m4_terminal_candle.py`, `_m5_completeness.py`, `_m7_check.py` · 8 new test files + the
fixtures in `tests/conftest.py` · `logs/m{2,3,4,5}_*.{txt,json}`, `logs/lot_schedule.txt`.
`logs/` and `data/raw/` stay git-ignored; every number here is re-runnable, none is retyped.

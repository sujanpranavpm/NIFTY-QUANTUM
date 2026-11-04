# DATA_FEASIBILITY_REPORT.md — P1, zero-cost NIFTY options data

Deliverable of phase **P1** ("Zero-cost data accuracy & feasibility implementation").
Every number below was produced by a script in `scripts/` during this phase and is
reproducible; the log it came from is named next to it. Where something could not be
checked with the resources allowed (₹0, no new accounts), it is labelled
`UNVERIFIED` rather than filled in with a plausible value.

Scope actually executed: Hugging Face archive inspection (file-by-file), official NSE
daily derivatives reconciliation, expiry-calendar verification against NSE circulars
and the exchange's own listed-expiry field, historical reference data (lot size,
strike interval, tick size), Cloud Trader Pro sample attempt, ICICI Breeze
documentation study, and the defect forensics that explain each anomaly.

Headline, in one paragraph: **the 1-minute NIFTY option prices in the HF archive are
real and reconcile to the exchange at the daily level — contract identity matches the
exchange exactly for every traded contract on all 15 audited days, and the daily close
equals the exchange's last traded price on 100% of contracts from 2025-01-30 onward — so
the archive is usable for 1m/5m/15m *price* research from 2021-05-07 to 2026-07-02.
Open interest is not usable as one continuous series: the archive changed its OI
convention on exactly 2025-01-01 (values became 1/100 of the exchange's) and by 2026
stopped carrying end-of-day OI for most contracts. Its intraday high/low also
under-covers the exchange's reported daily range from that same date, and no part of the
2021-05 → 2023-12 era can be verified at all because the free official archive begins
2024-01-02. Depth, bid/ask and per-minute trade counts exist in no permitted source.**

---

## A. Provider comparison

| Criterion | HF `thetrademarkk/india-index-options-1m` | NSE official F&O bhavcopy | NSE circulars + contract specs | Cloud Trader Pro | ICICI Breeze API |
|---|---|---|---|---|---|
| Data actually accessible | 440 Parquet files in the paths used (267 NIFTY, 62 BANKNIFTY, 148 SENSEX, 3 index); 18 fetched and read | 34-column daily CSV per trading day, ZIP, ~1.1–1.4 MB | PDF circulars + HTML spec tables | **None** — Drive files sit behind a Google sign-in | **None** — no credentials exist |
| Historical coverage | 2021-05-27 → 2026-08-04 (NIFTY); 2023-08-11 → 2026-07-30 (SENSEX); BANKNIFTY monthly only, → 2026-07-28 | **2024-01-02 → 2026-11-03 only**; earlier dates return 404 | Regime history from Nov-2022 onward (the three retrieved circulars) | Advertised "~2024 onward", unverified | SDK README claims "10 years of historical market data"; unverified |
| Granularity | 1 minute (whole-minute stamps, IST) | 1 trading day | n/a (rules, not series) | Advertised 1 minute | Documented `1minute`, `5minute`, `30minute`, `1day`, and `1second` streaming. **No documented 15-minute interval** |
| Contract availability | Every contract that the exchange reports as traded (verified 15/15 audited days); no contract the exchange did not list | All listed contracts incl. non-traded ones (with `TtlTradgVol = 0`) | Rule-level only | Unknown | Public master lists **only live listings** (0 expired NIFTY expiries) |
| Field availability | `timestamp, open, high, low, close, volume, open_interest, trading_day, symbol, strike, option_type, expiry`; index files drop `open_interest` | 34 fields incl. `OpnPric/HghPric/LwPric/ClsPric/LastPric/PrvsClsgPric/UndrlygPric/SttlmPric/OpnIntrst/ChngInOpnIntrst/TtlTradgVol/TtlTrfVal/TtlNbOfTxsExctd/NewBrdLotQty/XpryDt` | Lot sizes, strike intervals, price steps, expiry weekday regimes | Unknown | Documented: OHLC, `volume`, `oi`/`open_interest` for `NFO`; `open_interest` is `None` for cash equity |
| Data quality (measured) | OHLC internally valid (0 violations of high≥low, 0 nulls, 0 nonpositive prices in every audited file); **heavy whole-row duplication (74–91%)**; OI convention break; post-2025 range under-coverage | Self-consistent: `OI(t) = OI(t−1) + ChngInOpnIntrst(t)` holds on **100.0%** of contracts with residual 0.0 on all three consecutive-day pairs tested | Consistent with the archive's own `XpryDt`/`NewBrdLotQty` values | n/a | n/a |
| Usage restrictions | **CC-BY-NC-4.0** — non-commercial, attribution, no redistribution | Exchange public archive; use for verification, no redistribution claim needed | Same | CC0 **declared** in page JSON-LD; never confirmed on a file | Broker terms; account-bound; static-IP and rate rules |
| Authentication | Free HF account token | **None** (plain HTTPS; Akamai does not block `nsearchives`) | **None** | Google account required — that is what blocked it | AppKey + SecretKey + SessionToken + ICICI account with API enabled |
| Cost | ₹0 | ₹0 | ₹0 | ₹0 claimed, data never reached | ₹0 for the API itself, but needs a funded account; not attempted |
| Verdict for this project | **Primary 1-minute source** | **Primary daily reference** | **Primary rulebook** | Not usable at ₹0 without an account | Only revisit if the user already holds credentials |

Sources deliberately out of scope: any vendor requiring a subscription, any broker
needing a new account, and the NSE live option-chain JSON endpoints (current state
only, no history). See `DATA_SOURCE_REGISTER.md` §6.

---

## B. Actual schema report

Advertised schemas are **not** repeated here as results; only columns physically read
from Parquet/CSV are listed.

### B.1 Options files — the signature shared by all 16 inspected files

15 × `options/NIFTY/*.parquet` + 1 × `options/BANKNIFTY/2024-12-24.parquet`:

```
timestamp        timestamp[ns, tz=+05:30]      tz-aware IST, whole minutes
open             double
high             double
low              double
close            double
volume           int64
open_interest    int64
trading_day      string      'YYYY-MM-DD'
symbol           string      'NIFTY' | 'BANKNIFTY'
strike           int32
option_type      string      'CE' | 'PE'
expiry           string      'YYYY-MM-DD'
```

Written by `parquet-cpp-arrow version 21.0.0`, ZSTD codec, 9–14 row groups per file,
per-column null counts and min/max read from the footer statistics
(`scripts/_p1_hf_schema.py` → `logs/p1_hf_schema.txt`, `logs/p1_hf_schema.json`,
grouping reproduced by `scripts/_p1_schema_groups.py`).

Example of what the footer statistics show for the oldest file
(`options/NIFTY/2021-05-27.parquet`, 3,624,537 B, 333,365 rows): `timestamp` min
`2021-05-07 03:45:00+0000` (= 09:15 IST) max `2021-05-07 10:00:00+0000` (= 15:30
IST), `open/high/low/close` 2.35–2084.35 with 0 nulls, `volume` 75–77,852,
`open_interest` 75–3,802,350, `strike` 12,800–16,750, one `expiry`, one
`trading_day` per block.

### B.2 Index (spot) files — different schema, discovered not assumed

`index/NIFTY.parquet` (486,050 rows, 2021-05-24 → 2026-07-02) has **8 columns**:

```
timestamp, open, high, low, close, volume, trading_day, symbol
```

There is **no `open_interest` column at all**, and `volume` is `0.0` on every single
row (`min = max = 0.0`, `volume_zero_rows = 486050`). So the spot series is price
only. `scripts/_p1_content_audit.py` → `logs/p1_content_audit.txt`,
`scripts/_p1_forensics.py` → `logs/p1_forensics_3.txt`.

### B.3 Not inspected

`options/SENSEX/*`, `index/BANKNIFTY.parquet`, `index/SENSEX.parquet`, `mcx/*` and
`stocks_options/*` were enumerated by path and size but their columns were never
read. One planned sample, `options/SENSEX/2024-12-26.parquet`, does not exist in the
repository (HTTP 404) — a bad sample choice by this audit, not a dataset defect.
Any SENSEX schema statement would be unsupported, so none is made.

### B.4 Official NSE F&O bhavcopy — 34 columns, ISO-20022 naming

```
TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb, SctySrs,
XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp, FinInstrmNm, OpnPric, HghPric,
LwPric, ClsPric, LastPric, PrvsClsgPric, UndrlygPric, SttlmPric, OpnIntrst,
ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd, SsnId, NewBrdLotQty,
Rmks, Rsvd1, Rsvd2, Rsvd3, Rsvd4
```

`FinInstrmTp` values observed: `STO, IDO, STF, IDF` — index options are `IDO`
(`OPTIDX` never appears). NIFTY index options are therefore selected with
`TckrSymb == 'NIFTY' & FinInstrmTp == 'IDO'` (`scripts/_p1_nse_bhav_inspect.py`).

### B.5 ICICI Breeze security master — 69 columns, actually parsed

`FONSEScripMaster.txt` inside the public `SecurityMaster.zip`:

```
Token, InstrumentName, ShortName, Series, ExpiryDate, StrikePrice, OptionType,
CALevel, PermittedToTrade, IssueCapital, WarningQty, FreezeQty, CreditRating,
NormalMarketStatus, OddLotMarketStatus, SpotMarketStatus, AuctionMarketStatus,
… NormalMarketEligibility … IssueRate … MinimumLotQty, LotSize, TickSize,
CompanyName, ListingDate, ExpulsionDate, … SecurityExpiryDate,
NoDeliveryStartDate/EndDate, … OldToken, AssetInstrument, AssetName, AssetToken,
IntrinsicValue, ExtrinsicValue, ExcerciseStyle, … DeleteFlag, Remarks, BasePrice,
ExchangeCode
```

83,568 rows; `Series ∈ {OPTION (82,901), FUTURE (667)}`; `OptionType ∈ {PE 41,452,
CE 41,449, XX 667}`. **`ExpulsionDate`, `DeleteFlag` and `ReAdmissionDate` are empty
on every row and no expired listing is present**, which is why this file cannot be
used to address historical contracts (`scripts/_p1_breeze_master.py` →
`logs/p1_breeze_master.txt`).

---

## C. Historical coverage report

### C.1 What the archive contains (`scripts/_p1_hf_coverage.py` → `logs/p1_hf_coverage.txt`)

| Prefix | Files | Size | First | Last | Year counts | Weekday counts |
|---|---|---|---|---|---|---|
| `options/NIFTY` | 267 | 890.6 MB | 2021-05-27 | 2026-08-04 | 2021:31, 2022:52, 2023:52, 2024:52, 2025:52, 2026:28 | Thu 213, Tue 42, Wed 9, Mon 3 |
| `options/BANKNIFTY` | 62 | 276.2 MB | 2021-05-27 | 2026-07-28 | 2021:8, 2022:12, 2023:12, 2024:12, 2025:12, 2026:6 | Thu 39, Wed 12, Tue 10, Mon 1 |
| `options/SENSEX` | 148 | 360.8 MB | 2023-08-11 | 2026-07-30 | 2023:21, 2024:51, 2025:51, 2026:25 | Fri 69, Thu 41, Tue 34, Wed 4 |
| `index/` | 3 | 23.1 MB | — | — | NIFTY, BANKNIFTY, SENSEX | — |

BANKNIFTY has exactly 12 files/year — **one per month, no weeklies at all**. That is
not an inference from filenames: it was confirmed against the exchange (§C.3).

Largest inter-file gaps: NIFTY 2026-06-09 → 2026-07-07 (28 d), BANKNIFTY 2026-05-26 →
2026-07-28 (63 d), SENSEX 2026-05-21 → 2026-07-09 (49 d), NIFTY 2021-10-28 →
2021-11-11 (14 d), NIFTY 2025-10-14 → 2025-10-28 (14 d).

### C.2 Sessions and per-day completeness

A full NSE derivatives session is 09:15–15:30 IST. Audited expiry files show exactly
**376 distinct minute stamps per trading day** with first `09:15` and last `15:30`
(`logs/p1_forensics_1.txt`, `p1_forensics_2.txt`), i.e. both endpoints present — so
the last bar is either the 15:29–15:30 interval stamped at its end, or a closing
tick. The archive never states which, and the two conventions differ by one minute
for any backtest that acts "at the close". **Open question, not a defect claim.**

Out-of-session bars do exist, but only in some files: `index/NIFTY.parquet` has
**14,704 bars (3.03%)** outside 09:15–15:30 with first-bar times dominated by `09:07`
(844 days) and `09:00` (348 days) and last-bar times at `15:31` (698) and `15:59`
(276) — pre-open and post-close prints; `options/NIFTY/2025-01-30.parquet` has
**2,594 (0.38%)**; the option files audited for 2021-12 and 2022-09 have **0.00%**.

Per-contract sparsity is real and must not be confused with missing data: the audit
distinguishes "contract exists but traded few minutes" from "minute absent". The
truncated tail file `options/NIFTY/2026-08-04.parquet` covers only 2026-06-24 →
2026-07-02 (6 trading days, 172 contract-days, 46 strikes, 55 contracts) and
**contains no rows for its own expiry date at all**, so it cannot be reconciled on
either 2026-07-28 or 2026-08-04 (`logs/p1_nse_reconciliation.txt`, both blocks
`skipped`).

CE/PE balance in the audited files is close but not equal, e.g. that same file holds
PE 1,469 / CE 1,177 rows; strike spacing is dominated by 50 (38 of 45 gaps) with a
few 100s and one 350/500 — consistent with a near-expiry 50-point ladder plus
coarser far strikes.

### C.3 Coverage measured against the exchange, not asserted

Because the free official archive reaches back only to 2024-01-02, expiry coverage is
auditable from that date only. One bhavcopy per month from 2024-01 to 2026-12 was
downloaded and its `XpryDt` set (the exchange's own list of live expiries) compared
with the archive's file inventory (`scripts/_p1_nse_expiry_coverage.py` →
`logs/p1_nse_expiry_coverage.txt`):

```
NIFTY      official expiries audited:  143   missing INSIDE the archive span:  4 (2.8%)   after archive end: 15
BANKNIFTY  official expiries audited:   70   missing INSIDE the archive span: 36 (51.4%)   after archive end:  4
```

The four NIFTY holes are **2025-10-20, 2026-06-16, 2026-06-23, 2026-06-30**. The 36
BANKNIFTY holes are every weekly expiry the exchange listed during 2024-01 →
2024-11 (4–5 expiries per month were live then; the archive keeps exactly one per
month). SENSEX is excluded from this test on purpose: it is a BSE product and does
not appear in the NSE bhavcopy, so no free NSE-side arbiter exists.

Cross-check on individual dates (`scripts/_p1_nse_listed_expiries.py` →
`logs/p1_nse_listed_expiries.txt`): two BANKNIFTY dates that my hand-written regime
table called wrong (2024-03-27 Wed and 2024-12-24 Tue) were **confirmed by the
exchange** — the archive was right and my table was wrong; 2025-10-21 was never
listed (2025-10-20 was), so it is not a hole.

### C.4 Expiry-calendar cadence verified against primary circulars

Regimes were taken from the circulars' own text (`scripts/_p1_nse_circulars.py` →
`logs/p1_nse_circulars_text.txt`) and then tested (`scripts/_p1_expiry_calendar.py` →
`logs/p1_expiry_calendar.txt`), with the exchange archive as the trading-day arbiter:

| Symbol | Files | On-regime-weekday | Holiday-shift deviations | Unexplained | Unverifiable (pre-2024) |
|---|---|---|---|---|---|
| NIFTY | 267 | 255 / 267 | 7 | **0** | 5 |
| BANKNIFTY | 62 | 56 / 62 | 1 | 2 (both later confirmed by the exchange) | 3 |
| SENSEX | 148 | 139 / 148 | 8 | 1 (2025-01-03 Fri) | 0 |

Verbatim regime content: NIFTY weekly availability reduced from 7 to 4 contracts from
09-Dec-2022 with **no weekday change** (FAOP/54334); from 20-Nov-2024 only one
benchmark index per exchange keeps weeklies and BANKNIFTY/FINNIFTY/MIDCPNIFTY weeklies
are discontinued (FAOP/64506); NIFTY weekly **and** monthly move to Tuesday from
01-Sep-2025, with the annexure itself writing holiday shifts such as "24-Dec-25
(since 25-Dec-25 is holiday) → 30-Dec-25" (FAOP/68747). The archive's weekday census
matches this: NIFTY Thu 213 → Tue 42 concentrated at the 2025-09 boundary.

Seven NIFTY deviations were each confirmed as a genuine holiday shift by the absence
of a bhavcopy on the nominal weekday: `2024-04-10` (Wed) instead of Thu 04-11,
`2024-08-14` instead of 08-15, `2025-04-09` instead of 04-10, `2025-04-30` instead of
05-01, `2026-03-02` (Mon) instead of 03-03, `2026-03-30` instead of 03-31, `2026-04-13`
instead of 04-14. Five older Wednesday deviations (2021-08-18, 2022-04-13, 2023-01-25,
2023-03-29, 2023-06-28) cannot be tested at all because no bhavcopy exists before
2024-01-02 — they are reported as unverifiable, not as correctness. The earlier claim
that "NIFTY weeklies were suspended Nov-2022 → Nov-2023" is **retracted**: FAOP/54334
reduces the count from 7 to 4, not to zero, and the weekday never moved.

---

## D. Data accuracy report

Each item names the number that produced it. "Audited blocks" means the 15 dated
NSE-reconciliation blocks (one mid-life and one expiry day for each of 8 expiries, plus
one extra pair), 6 of them in 2024 and 9 in 2025/2026 — `scripts/_p1_nse_reconcile.py` →
`logs/p1_nse_reconciliation.txt`.

### D.1 Passed checks

| Check | Scope | Result | Evidence |
|---|---|---|---|
| Timezone and stamp alignment | 16 option files + `index/NIFTY.parquet` | `tz = UTC+05:30` in every file; `is_whole_minute = True` in every file; 0 null timestamps | `logs/p1_content_audit.txt` |
| OHLC internal validity | same 17 files (largest 675,497 rows) | `null_*` = 0, `nonpositive_*` = 0, `ohlc_order_violations` = 0 in **every** file — no `high < low`, no zero or negative prices | same |
| Duplicate timestamps are whole-row only | the 6 files that contain duplicates | mean rows per affected minute exactly **2.00**; affected minutes where prices differ **0**; where OI differs **0**; dedup restores the true count (572,335 → 312,488; 624,161 → 354,738; 675,497 → 425,716) | `logs/p1_forensics_1.txt`, `p1_forensics_2.txt` |
| Contract identity vs the exchange | 15 audited blocks | `hf_only_contracts = 0` on **15/15** and `matched_contracts = official_contracts_traded` on 15/15 (best case 2026-02-17: 202 matched = 202 traded, nothing one-sided). The archive never invents a contract and never misses a traded one | `logs/p1_nse_reconciliation.txt` |
| Daily close vs exchange | 15 blocks | `hf_close == LastPric` **exactly** on 100.0% of contracts in every block from 2025-01-30 onward; 94.42–98.75% in the 2024 blocks. Deliberately **not** compared with `ClsPric` (3.89–43.04% match ⇒ not the last trade) or `SttlmPric` (median gap ≈ the whole underlying ⇒ not a price of the option) | same |
| Volume unit | 15 blocks | median(day Σ`volume` ÷ `TtlTradgVol`) = the board lot **exactly** in all 15 (50.0002 / 50.001 / 25.0001 / 25.0002 / 25.0 / 75.0 / 65.0) ⇒ archive volume is **share-equivalent**; contracts = volume ÷ lot | same, `logs/p1_nse_oi_units.txt` |
| Official reference is internally sound | 3 consecutive-day pairs (n = 1,702 / 1,629 / 1,608) | `OI(t) = OI(t−1) + ChngInOpnIntrst(t)` holds for **100.0%**, median and p90 absolute residual **0.0** ⇒ bhavcopy OI is fit to be the authority | `logs/p1_oi_break.txt` |
| Price grid | 7 archive files (10,584–2,701,988 prices), 60 official dates | archive 100.00% on the ₹0.05 grid, **0.00% off-grid**; official *traded* columns (`Opn/Hgh/Lw/Cls/LastPric`) also 0.00% off-grid on every date tested through 2026-08-10 | `logs/p1_tick_grid.txt`, `logs/p1_tick_change.txt` |
| Expiry weekday vs regime history | 267 NIFTY files | 255 on the regime weekday, 7 holiday shifts (each confirmed by a 404 bhavcopy on the nominal day), **0 unexplained** | `logs/p1_expiry_calendar.txt` |
| Historical reference data obtainable | 66 official dates 2024-01-02 → 2026-11-03 | `NewBrdLotQty` present and single-valued per era {50 → 25 → 75 → 65}; `StrkPric` ladder 50-pt near ATM with 500/1000/1500-pt far strikes; `XpryDt` gives the exchange's live-expiry list every day | `logs/p1_reference_data.txt` |

### D.2 Failed checks

**F1 — the archive changed its OI unit on exactly 2025-01-01.**
`official_OI ÷ archive_OI` measured day by day across three weekly expiries
(`scripts/_p1_oi_boundary.py`):

```
2024-12-30  ratio 0.998    2024-12-31  ratio 0.996   │ contracts
2025-01-01  ratio 100.000  2025-01-02  ratio 99.424  │ 1/100 of contracts
```

The break is also visible inside the archive alone, with no exchange data involved:
median stored OI per file falls from **271,700** (`2024-12-26`) to **2,814** (`2025-01-30`)
and median OI ÷ the file's own volume from **0.63** to **0.004** (`logs/p1_oi_break.txt`).
Repair: multiply by 100 when `trading_day >= 2025-01-01`. After the repair the *magnitude* is
right — ratio median 99.42–100.34, within 1% on **54.95–87.22%** of the contracts that still
carry an OI value (`logs/p1_oi_repair.txt`) — but see F2 for whether a value exists at all.

**F2 — end-of-day OI becomes unstable and then largely disappears.**
Share of exchange-verified traded contracts whose archive OI for the day is nonzero:
**50.9–100%** through 2024-12-31, still **100.00%** on 2025-01-23 and **100.00%** on
2025-03-20, but **41.18%** (2025-01-30), **46.77%** (2025-09-23), **50.56%** (2025-09-30),
**11.88%** (2026-02-17) and **9.57%** (2026-02-24) — so the defect is not a clean monotone
decay; it is intermittent in 2025 and near-total loss by 2026 (`logs/p1_oi_boundary.txt`,
`p1_oi_repair.txt`).
At file level the same decay: `oi_zero_rows` = 0–4 rows in every pre-2025 file versus
137,970 / 210,511 / 216,147 / 295,876 / 436,639 rows (**64.6–66.3%** of all rows) in the
post-2025 files (`logs/p1_content_audit.txt`). The daily **maximum** OI stays populated but is
*not* the end-of-day value (official ÷ peak ratio medians 44–98), so it cannot be substituted.
Consequence: post-2025 EOD OI must come from the bhavcopy joined on
(date, expiry, strike, type). Low presence on expiry days (24.7–37.4%) is a separate and
legitimate effect — the exchange itself reports OI 0 once the contract has expired.

**F3 — from the same date the archive's intraday range under-covers the exchange.**
`violations_hf_high_below_official` counts traded contracts whose archive day-max sits below
the exchange's `HghPric` (tolerance ₹0.005); `…low_above_official` is the mirror.

| Era | high-violations (count) | as % of audited contracts | low-violations | `open` exact | `high` exact | `low` exact |
|---|---|---|---|---|---|---|
| 2024 blocks | 0, 1, 3, 1, 0, 1 | ≤ 1.31% | 0 in all 6 | **100.0%** (all 6) | 98.70–100.0% | 100.0% |
| 2025/2026 blocks | 66, 45, 69, 37, 50, 50, 31, 84, 56 | **15.7–44.7%** | 15–31 each (10.0–17.2%) | 50.53–75.00% | 55.32–84.32% | 82.78–90.00% |

The same pipeline change as F1, and it is directionally one-sided: the archive's range is
*narrower* than the exchange's, i.e. real trades are missing rather than invented. A strategy
whose entry depends on an intraday extreme (spike, wick, stop hunt) is therefore weaker
evidence after 2025-01-01. A daily report cannot verify an individual minute, so the claim is
deliberately framed as the provable one: *the archive's own day extremes contradict the
exchange's*.

**F4 — the index (spot) series carries no volume and no OI.** `volume min = max = 0.0` on all
**486,050** rows of `index/NIFTY.parquet`, and the file has no `open_interest` column at all
(8 columns, §B.2). Usable only as a price path.

**F5 — the newest NIFTY file is truncated.** `options/NIFTY/2026-08-04.parquet` holds **2,646**
rows covering 2026-06-24 → 2026-07-02 (6 trading days, 55 contracts, 46 strikes) and contains
**no rows for its own expiry day**, so it reconciles on neither 2026-07-28 nor 2026-08-04 (both
blocks `skipped`). Last usable data date: **2026-07-02**.

### D.3 Warnings

- **W1 out-of-session bars exist.** `index/NIFTY.parquet` **14,704 bars (3.03%)** outside
  09:15–15:30 (first-bar times `09:07` ×844, `09:00` ×348; last-bar `15:31` ×698, `15:59` ×276,
  plus stray 17:52/18:07/13:30). `options/NIFTY/2025-01-30.parquet` **2,594 (0.38%)**, at
  15:42–15:46 with `volume = 0` and flat OHLC — post-close stale rows, not trades. `2023-12-28`
  and `2024-12-26` each contain one day whose first bar is stamped 18:15/18:00 and whose last is
  19:15/19:00. Rule: filter to 09:15–15:30 explicitly; never
  delete or interpolate a bar.
- **W2 minute coverage per day is not uniformly 376.** Distinct minute stamps per trading day:
  median 376.0 in `2021-12-30`, `2022-09-29`, `2025-09-02`, `2026-02-24` (377.0 in `2025-01-30`,
  because of W1's post-close stamps), but **238.5** in `2024-01-25`, **285** in `2025-03-27`,
  **301** in `2025-09-30`, **359** in `2024-12-26`, with per-file minima of **1**
  (`2021-05-27`), 61, 76 and 173. Contract-level absence is mostly *no trade in
  that minute* (correct behaviour, and it must stay absent), but whole-minute absence at file
  level occurs and must be measured per file before any completeness claim.
- **W3 the last-bar convention is unresolved and inconsistent.** Audited files show 376 stamps
  running `09:15 … 15:30` with both endpoints present, yet `2022-09-22` ends at **15:29** with
  375 stamps and 9 of 42 days in `2023-12-28` end at 15:29. So the terminal bar is sometimes
  the 15:29–15:30 interval and sometimes a 15:30 closing print — a one-minute ambiguity for any
  rule that acts "at the close". This is an open question, not a defect claim: the archive
  documents no convention.
- **W4 zero-range bars are common.** `open = high = low = close` on 191,121/675,497 (**28.3%**)
  rows in `2025-01-30`, 149,905 in `2023-12-28`, 12,052/486,050 (**2.5%**) in the index file.
  These are carry-forward/no-trade minutes; treating them as trades would fake fill realism.
- **W5 `volume = 0` rows appear only after the 2025 change:** 5,362 / 3,691 / 1,155 / 992 / 742
  / 41 rows in the post-2025 files (with `volume_min = 0.0`), versus `volume_zero_rows = 0` in
  every pre-2025 file. Pre-2025 minima are lot-sized (25/50/75) except two files that reach
  1.0, so per-bar lot-multiplicality is **unverified** even though the daily aggregate ratio is
  exact.
- **W6 coverage holes carried forward from §C:** NIFTY 4 of 143 exchange expiries absent inside
  the archive span (2025-10-20, 2026-06-16, 2026-06-23, 2026-06-30); BANKNIFTY 36 of 70 (every
  2024 weekly); inter-file gaps up to 63 days.
- **W7 "final bar of the day" depends on row order.** F2 and the OI ratios aggregate `.last()`
  within a trading day, which assumes the file is time-ordered; row order is not documented as
  sorted, so EOD presence should be read as "an OI value for that day". The **unit** conclusion
  (F1) is immune to this — a median ratio of 0.996 versus 100.000 cannot be produced by ordering.

### D.4 Conflicting records

- **C1 documented tick slab vs observed grid.** NSE's contract-specifications page keys the
  price step on the **index level** (0–15,000 → 0.05; >15,000–30,000 → 0.10; >30,000 → 0.20,
  NSE/FAOP/67135), which would put NIFTY options on ₹0.10 for the whole audited history (index
  21,352–25,986). Measured instead: official traded prices **0.00% off the ₹0.05 grid** on every
  date through 2026-08-10, and the archive **100.00% on it**. Third, independent evidence agrees
  with the measurement — the Breeze security master carries `TickSize = 5` on **3,388 of 3,391**
  NIFTY rows and `10` only on its 3 *futures* rows, i.e. the band rule is applied where the
  circular's own footnote says it applies. **Adopted: ₹0.05 for NIFTY option premiums**, chosen
  because two independent observed sources agree while the slab is rule text, not data.
- **C2 NSE changed its settlement computation on 2026-08-03.** From that date the *derived*
  bhavcopy columns (`PrvsClsgPric`, `SttlmPric`) go from 0.00% to **20.14–23.43%** off the ₹0.05
  grid while the traded columns stay **0.00%** off (`logs/p1_tick_change.txt`). The archive is
  not implicated. Note on method: `logs/p1_tick_grid.txt` reports 6–9% "off-grid" for those
  dates only because it unions traded and derived columns; that earlier reading, which suggested
  the exchange had refined its tick, is **superseded** by the column-separated result. Any rule
  using settlement prices after 2026-08-03 must not assume a 0.05 grid.
- **C3 my hand-written expiry-regime table contradicted the exchange three times.** `XpryDt`
  confirmed the archive on BANKNIFTY 2024-03-27 (Wed) and 2024-12-24 (Tue), showed 2025-10-21 was
  never listed, and FAOP/54334 disproved the "weeklies suspended Nov-2022 → Nov-2023" note (§C.3,
  §C.4). Standing rule for P2: bhavcopy `XpryDt` is the arbiter of what existed, circulars explain
  *why*, and no hard-coded weekday or lot-size table may override either — which is precisely why
  the ingestion layer must not assume today's expiry weekday or lot size across history.
- **C4 dataset documentation vs files on disk.** The declared Open Interest field **is** physically
  present in every audited option file (`int64`, §B.1), so the presence half of the discrepancy is
  resolved in the archive's favour and the value half against it (F1/F2). Where the documentation's
  implied uniformity fails: index files have no such column at all (F4), BANKNIFTY carries one
  expiry file per month with no weeklies (§C.1), and SENSEX begins 2023-08-11 rather than 2021.

### D.5 Unverified and unavailable

| Item | Status | Why |
|---|---|---|
| 1-minute prices / volume / OI / calendar **2021-05 → 2023-12** | **UNVERIFIED** | the free official archive begins 2024-01-02 (every earlier date 404), so no arbiter exists at ₹0 — single-source only |
| Minute-level OI *path* (not just EOD) | **UNVERIFIED** | no free source publishes per-minute OI; the archive was tested for internal consistency only — 17.15–33.41% of bars change OI, decreases occur, never monotonic, OI ÷ cumulative-volume median scattered 0.16–2.00 across files |
| Bid/ask quotes, market depth, per-minute trade count | **UNAVAILABLE** | absent from the schema (§B.1); `TtlNbOfTxsExctd` exists daily in the bhavcopy from 2024 only |
| Greeks, volatility surface | **UNAVAILABLE** — must be computed | no source provides them; computing them needs the index path (with W1/W4 handling) plus an explicit rate assumption |
| Cloud Trader Pro samples | **UNVERIFIED — zero bytes retrieved** | the Drive ids return HTTP 401 / a Google sign-in interstitial anonymously; opening an account is forbidden, so no data-quality claim is made in either direction |
| ICICI Breeze historical charts | **UNVERIFIED — documented only** | needs an ICICI account plus AppKey/SecretKey/SessionToken; no API was called. Its public security master carries **only live listings** (3,391 NIFTY rows over 9 expiries, 2026-11-09 → 2027-06-29, zero expired), so it cannot map historical tokens |
| NSE holiday lists before 2024 | **NOT ATTEMPTED** | holidays are inferable indirectly (404 on a date), but the published annual lists were not fetched; recorded as a gap, not as knowledge |

---

## E. Research feasibility

Verdicts are for **NIFTY index options**, the mission instrument. Each answer names the
condition that makes it true, because none of them is unconditional.

**1. Is one-minute NIFTY options research feasible using free sources?**
**YES for price research, NO for OI-driven or liquidity-driven research after 2025.**
Usable span **2021-05-07 → 2026-07-02** (267 expiry files, 890.6 MB, ~376 minute stamps per
session), prices internally perfect (§D.1) and reconciling to the exchange on contract
identity 15/15 and on daily close to 100% exact (§D.1, F3). Conditions: deduplicate on
(symbol, expiry, strike, option_type, timestamp) — lossless (§D.1); restrict to 09:15–15:30
(W1); treat 2021-05 → 2023-12 as **unverifiable at ₹0** (§D.5). Not feasible where minute OI or
depth is the signal (F1/F2, §D.5).

**2. Is five-minute research sufficiently supported?**
**YES — by resampling the 1-minute series, not by sourcing 5m separately.** No approved
zero-cost source ships native 5m NIFTY option history (Breeze documents `5minute` but needs an
account, §D.5). Aggregating the verified 1m bars gives 5m bars whose *close* inherits the
100%-exact property and whose *extremes* inherit F3's post-2025 under-coverage. Resampling is
correct for anything that acts on close/open of the interval and degraded for wick-based rules
after 2025-01-01.

**3. Is fifteen-minute research sufficiently supported?**
**YES, same derivation and same caveat.** Note for the record: Breeze documents
`1minute / 5minute / 30minute / 1day` — **no 15-minute interval exists** in the documented API,
so 15m must be derived from 1m here and would have to be derived from 1-minute data on any
future provider too. 376 minute stamps per session divide cleanly into 15-minute blocks only if
the terminal-bar convention (W3) is fixed first.

**4. Is weekly expiry coverage adequate?**
**YES for NIFTY.** The archive holds 267 NIFTY expiry files spread over the two verified regimes
(213 Thursday-dated up to 2025-08, 42 Tuesday-dated from 2025-09, plus 9 Wednesday and 3 Monday
holiday shifts), i.e. **52 files per year ≈ 4.3 expiries per month**, which is exactly the
exchange's live-expiry count — and the exchange's own `XpryDt` list confirms **139 of its 143
audited expiries are present** (§C.3). A weekly-expiry strategy must additionally
handle the Thursday→Tuesday switch from 2025-09-01 and the holiday shifts (§C.4). Not adequate
for **BANKNIFTY** weeklies: every 2024 weekly is absent (36 of 70 expiries missing) — outside
the mission instrument, but it disqualifies that symbol.

**5. Is monthly expiry coverage adequate?**
**YES.** Monthly-dated expiries are present throughout, and long-dated files carry full lives
(up to **43 trading days** — e.g. `2025-03-27` covering 2025-01-27 → 2025-03-27, `2024-01-25`
covering 2023-11-28 → 2024-01-25) while weekly files span 6–9 days, so both short- and
long-dated contracts are actually available and not just listed as filenames. Long-dated
*intraday liquidity* was deliberately not measured: with a daily report as the only arbiter, "no
trades in that minute" and "pipeline gap" are not separable (W2), so a monthly/quarterly strategy
must measure per-file minute completeness in P2 rather than assume a continuous tick.

**6. Can ATM and nearby strikes be selected historically?**
**YES.** `strike` (int32), `option_type` (CE/PE), `expiry` and `trading_day` are present and
verified against the exchange's `StrkPric`/`OptnTp`/`XpryDt` on every audited block (§D.1), and
the underlying level needed to define ATM is available at **1-minute** resolution from
`index/NIFTY.parquet` (2021-05-24 → 2026-07-02, price only — F4) and daily from `UndrlygPric`
(2024+). Observed ladders are **50-point** around ATM in every audited expiry (128–130 of the
gaps) with 500/1000/1500-point steps far out, and the 50-point band narrows near expiry
(`2026-08-04`: 38 of 45 gaps). Because spacing depends on the index level *and* on
days-to-expiry, ATM/nearby must be computed per (date, expiry) from the file's own strike set —
never from a fixed interval.

**7. Is historical OI genuinely available?**
**PARTIALLY — and this is the project's main data limitation.**
*Daily EOD OI:* **VERIFIED** from the official bhavcopy for 2024-01-02 → present (identity holds
100.0%, §D.1); **unavailable free for 2021-05 → 2023-12**.
*Minute OI from the archive:* **CORROBORATED** through 2024-12-31 (official ÷ archive median
0.996–1.001), **ESTIMATED** from 2025-01-01 (×100 puts the magnitude within 1% on 54.95–87.22%
of the contracts that still carry a value), and effectively **LOST** for most contracts by 2026
(nonzero presence 9.57–11.88%; 64.6–66.3% of rows are zero) — F1/F2. Practical rule: after
2025-01-01, OI features must be built from the daily join, and intraday OI must be labelled an
estimate in any strategy report.

**8. Can the data support realistic options backtesting?**
**PARTIALLY — yes for decision logic and contract accounting, no for fill realism.**
Supportable now: CE/PE entries and exits on 1m/5m/15m prices with the correct historical
contract universe (§D.1), lot sizes taken per date from `NewBrdLotQty` (50 → 25 → 75 → 65),
₹0.05 price grid, expiry weekday / regime calendar verified against primary circulars, and
expiry-day settlement values from `SttlmPric` (2024+, with the C2 grid change).
Not supportable: **bid/ask spread, depth, per-minute trade count, and therefore any calibrated
slippage or capacity limit** (§D.5). A backtest must therefore *assume* a spread/slippage model
and report it as an assumption, and the existing equity cost model in `nifty_quant/engine/costs.py`
cannot be reused for options without building and validating an options-specific cost model
(STT/transaction charges on premium, settlement and pin risk) in P2. Plus the two era-dependent
weaknesses: F3 (missing intraday trades post-2025) and F2 (no reliable intraday OI).

**9. What accuracy limitations remain?**
(1) 2021-05 → 2023-12 has **no arbiter at all** — every claim for that era is single-source.
(2) Post-2025 the archive misses intraday trades on 15.7–44.7% of audited contracts, so
extremes and volume are under-stated (F3, W5).
(3) Minute-level OI is unverified everywhere and unusable after 2025 for most contracts (F1/F2).
(4) No bid/ask/depth/trade count anywhere → no spread, slippage or capacity validation (§D.5).
(5) Terminal-bar convention unresolved, and inconsistent across files (W3); out-of-session and
zero-range/stale rows must be filtered, not trusted (W1/W4).
(6) Four NIFTY expiries absent inside the span and the newest file truncated, so live data stops
at 2026-07-02 (W6/F5).
(7) SENSEX cannot be checked against any free NSE-side report at all (§C.3).
(8) EOD OI presence was measured with a row-order-dependent aggregation (W7).
(9) Exchange-side definitions changed mid-history (settlement computation 2026-08-03, C2), so a
single rule cannot span the whole archive without date-conditional logic.

**10. Are additional free providers necessary?**
**Not to start, and none of them currently closes the gaps.** Everything answerable at ₹0 has
been measured; the primary archive plus the official bhavcopy is sufficient to begin price-based
NIFTY options research. The two open questions that a second source *would* close are (a) the
post-2025 intraday completeness regression (F3) and (b) verification of 2021-05 → 2023-12. The
remaining approved free providers cannot do it: **Cloud Trader Pro** sits behind a Google sign-in
(HTTP 401, zero bytes obtained without opening an account) and **ICICI Breeze** needs an existing
funded account, while its public security master lists only live contracts, so it cannot even
address history. Recommendation: proceed to P2 on the verified fields, and treat "a second
independent 1-minute source" as an **open requirement, not a blocker** — to be revisited only if
a no-account sample becomes reachable, or if the user decides to spend ₹0-boundary-breaking money.

---

## F. Final recommendation — field-by-field classification

Classifications: **VERIFIED** = confirmed against an independent authority. **CORROBORATED** =
confirmed by two sources agreeing. **SINGLE SOURCE** = internally consistent, no arbiter.
**PARTIAL** = present but with measured, dated gaps. **ESTIMATED** = derived by an assumption.
**UNAVAILABLE** = does not exist in any permitted source.

| Required field | Source | Classification | Usable from | Evidence / condition |
|---|---|---|---|---|
| `timestamp` (1m, IST) | HF archive | **CORROBORATED** | 2021-05-07 | tz `+05:30`, whole-minute, 0 nulls in 17 files; session bounds 09:15/15:30 match NSE trading hours; W3 convention caveat on the terminal bar |
| `open` / `high` / `low` (premium) | HF | **CORROBORATED 2024-01 → 2024-12** · **PARTIAL 2025-01 →** · **SINGLE SOURCE 2021-05 → 2023-12** | 2021-05-07 | 2024 blocks: open 100.0%, high 98.70–100.0%, low 100.0% exact vs `OpnPric/HghPric/LwPric`; 2025+ blocks: 50.53–84.32% exact and day-range too narrow on 15.7–44.7% of contracts (F3) |
| `close` (premium) | HF | **VERIFIED** | 2021-05-07 | equals the exchange's `LastPric` exactly on 100.0% of contracts in every 2025/2026 block, 94.42–98.75% in 2024; never compared with `ClsPric`/`SttlmPric` (§D.1) |
| `volume` (1m) | HF | **CORROBORATED** (unit proven) · **PARTIAL 2025+** | 2021-05-07 | daily aggregate ratio to `TtlTradgVol` = the board lot exactly in 15/15 blocks ⇒ share-equivalent; must be divided by `NewBrdLotQty` for contracts; post-2025 missing trades + `volume = 0` rows (F3/W5); per-bar lot-multiplicality unverified |
| `open_interest` (1m) | HF | **CORROBORATED ≤ 2024-12-31** · **ESTIMATED ≥ 2025-01-01** (×100) · **PARTIAL/lost presence** | 2021-05-07, values only | ratio 0.996–1.001 before the break, 100.000 after (F1); ×100 lands within 1% on 54.95–87.22% of contracts that still carry a value; presence falls to 9.57–11.88% by 2026 (F2) |
| `open_interest` at EOD (reference) | NSE bhavcopy | **VERIFIED 2024-01-02 →** · **UNAVAILABLE 2021-05 → 2023-12** | 2024-01-02 | `OpnIntrst` + `ChngInOpnIntrst` identity holds 100.0% with residual 0.0; join on (date, expiry, strike, type); post-2025 this is the OI source of truth |
| `trading_day`, `symbol`, `expiry`, `strike`, `option_type` | HF | **VERIFIED** | 2021-05-07 | contract identity matched the exchange 15/15 blocks with 0 archive-only contracts, and the exchange's `XpryDt` confirmed 139/143 expiries (§C.3) |
| Underlying NIFTY 1m level (for ATM, moneyness, greeks) | HF `index/NIFTY.parquet` | **SINGLE SOURCE** (price only) | 2021-05-24 | 8 columns, 486,050 rows, 2021-05-24 → 2026-07-02; **no `open_interest` column and `volume = 0` on every row** (F4); 3.03% out-of-session bars must be filtered (W1) |
| Daily underlying close / previous close | NSE bhavcopy | **VERIFIED** | 2024-01-02 | `UndrlygPric`, `PrvsClsgPric` (note the 2026-08-03 grid change, C2) |
| Expiry calendar + weekday regimes | NSE `XpryDt` + circulars + archive | **VERIFIED 2024-01 →** · **PARTIAL 2021-05 → 2023-12** | 2024-01-02 for verification | 255/267 on regime weekday, 7 confirmed holiday shifts, 0 unexplained; 5 pre-2024 deviations unverifiable; FAOP/54334, /64506, /68747 supply the regime text (§C.4) |
| Trading holidays | NSE report availability | **VERIFIED 2024-01 →** · **UNAVAILABLE before** | 2024-01-02 | a 404 bhavcopy = holiday, `Range: bytes=0-3` + `PK` magic = trading day; published pre-2024 lists not fetched (§D.5) |
| Historical lot size | NSE `NewBrdLotQty` | **VERIFIED 2024-01 →** · **ESTIMATED 2021-05 → 2023-12** | 2024-01-02 | per-date values {50 → 25 (from 2024-05) → 75 (from 2024-12) → 65 (from 2025-11)}; pre-2024 only inferable from the archive's own minimum bar volumes (75, 50) ⇒ must be confirmed in P2, never carried back blindly |
| Historical strike interval | NSE `StrkPric` + archive | **VERIFIED 2024-01 →** · **SINGLE SOURCE before** | 2024-01-02 | 50-pt near ATM, 500/1000/1500-pt far; varies with index level and days-to-expiry ⇒ compute per (date, expiry) |
| Tick size / price grid | HF + NSE traded prices + Breeze master | **CORROBORATED at ₹0.05** (documented slab conflicts) | 2021-05-07 | archive 100.00% on-grid, official traded 0.00% off-grid through 2026-08-10, Breeze `TickSize = 5` on 3,388/3,391 NIFTY rows; the index-level slab table is the rejected reading (C1) |
| Contract identifiers (token map) | archive keys / Breeze master | **PARTIAL** | — | the archive has no exchange token, only (symbol, expiry, strike, type) — sufficient for 15/15 matches; no free source provides *historical* tokens (Breeze master is live-only, 0 expired listings) |
| Expiry-day settlement value | NSE `SttlmPric` | **VERIFIED** (not a candle) | 2024-01-02 | used only for settlement/pin handling, explicitly excluded from close comparisons; computation changed 2026-08-03 (C2) |
| Bid / ask / market depth | — | **UNAVAILABLE** | — | absent from every permitted source actually reached; no spread can be measured |
| Per-minute trade count | — | **UNAVAILABLE** (daily `TtlNbOfTxsExctd` from 2024 only) | — | §D.5 |
| Greeks / IV surface | computed | **UNAVAILABLE** as data | — | derivable from verified premium + underlying + verified lot/expiry calendar, but needs an explicit rate assumption and inherits W1/W4 |
| Cloud Trader Pro 1m samples | — | **UNVERIFIED** | — | HTTP 401 sign-in wall; zero bytes retrieved; no claim either way |
| Breeze 1m/5m/30m/1d charts | — | **UNVERIFIED (documented only)** | — | all endpoints, intervals, fields, 100 calls/min + 5,000 calls/day and the auth chain are recorded as *documented*, nothing was called (§DATA_SOURCE_REGISTER §5) |

### Recommended decision

**Proceed to P2 (ingestion + options backtest capability) on this data set — with the era rules
below enforced in code, and with liquidity realism explicitly out of scope until a second source
exists.** The price series is the strongest part (identity 15/15, close exact 100%, OHLC
internally perfect, grid correct); OI and intraday completeness are the weak parts and both
break on the same date.

Non-negotiable ingestion rules that follow directly from the evidence:

1. Deduplicate on (symbol, expiry, strike, option_type, timestamp) — proven lossless (§D.1).
2. Filter bars to 09:15–15:30 IST; keep absent minutes absent; **never forward-fill or
   interpolate** (W1/W4).
3. `open_interest × 100` **iff `trading_day >= 2025-01-01`**, and label the result an estimate
   (F1); take authoritative EOD OI from the bhavcopy join for 2024+ (F2).
4. Convert `volume` to contracts by dividing by the **date's own** `NewBrdLotQty` (50/25/75/65),
   never a constant.
5. Derive 5m and 15m by resampling the cleaned 1m series; fix the terminal-bar convention (W3)
   with an explicit, tested rule before using a closing bar.
6. Read the strike ladder, ATM set and expiry weekday per (date, expiry) from the data, never
   from a hard-coded table (§C3 rule).
7. Stamp every result with its era: *verified* (2024-01-02 → 2026-07-02), *corroborated-but-*
   *range-degraded* (2025-01-01 →), or *single-source, unverifiable* (2021-05-07 → 2023-12-31).

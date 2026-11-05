# DATA_SOURCE_REGISTER.md

Every data source that this project has been pointed at, what was actually done with
it, and what it is allowed to be used for. Nothing in this file is inherited from a
provider's marketing page: the `Status` column describes what was **observed**, and
`Evidence` names the artifact that shows it.

Written at P1 (2026-11-04), units and lot rows refreshed by the data-accuracy corrections
(2026-11-05), free-source assessment and readiness audit added at P2 (2026-11-05). Companion
documents: `DATA_FEASIBILITY_REPORT.md`, `DATA_ACCURACY_CORRECTIONS.md` (conclusions),
`P2_READINESS_AUDIT.md` (P2 gate + 13-part acceptance + §14 reconciliation appendix),
`PROJECT_STATUS.md` (repository state).

## Rules this register enforces

1. A source may not be used for research until its licence permits it (see `Licence`,
   `Permitted use`).
2. A capability may not be claimed until it has been seen in a real file or a real
   response. Documented-but-unobserved capability is registered as `UNVERIFIED`.
3. Raw downloads live under `data/raw/` and are git-ignored. No provider data is
   committed.
4. Fields from different providers are never merged into one series. Cross-provider
   work is comparison only.

---

## 1. Hugging Face `thetrademarkk/india-index-options-1m`

| | |
|---|---|
| Role | Primary 1-minute historical source (spot indices + index options) |
| URL | `https://huggingface.co/datasets/thetrademarkk/india-index-options-1m` |
| Licence | `cc-by-nc-4.0` (read from the dataset card tags, not assumed) |
| Permitted use | Non-commercial research only. Raw files stay private; attribution required if anything derived is published. **No redistribution, no selling, no commercial deployment.** |
| Access | Public repo, `private=False`, `gated=False`. **Measured in P1: the files were fetched anonymously** over `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with a plain User-Agent — no token was used or needed, no account was created, no paid tier touched. (`huggingface_hub` 1.7.1 is installed but was never called; `HF_TOKEN` is unset and `~/.cache/huggingface/token` does not exist, which is how this was checked.) Corrected 2026-11-04: an earlier row here claimed a free account token was used, which was not true |
| Structure observed | `index/{SYMBOL}.parquet` (3 files), `options/{SYMBOL}/{EXPIRY}.parquet` (NIFTY 267, BANKNIFTY 62, SENSEX 148), plus `mcx/` and `stocks_options/` (210 symbols) not used |
| Volume observed | `options/NIFTY` 890.6 MB, `options/BANKNIFTY` 276.2 MB, `options/SENSEX` 360.8 MB, `index/` 23.1 MB |
| Card's own caveat | "Educational use only ... Verify against official exchange data" and "Option coverage is partial: illiquid/far strikes are often sparse or absent" |
| Status | **Inspected file-by-file.** Schema, coverage, duplicates, session bounds, OHLC validity and price agreement all measured |
| Evidence | `scripts/_p1_hf_survey.py` → `logs/p1_hf_survey.txt`; `_p1_hf_schema.py` → `logs/p1_hf_schema.txt/.json`; `_p1_hf_coverage.py` → `logs/p1_hf_coverage.txt/.json`; `_p1_content_audit.py` → `logs/p1_content_audit.txt/.json`; `_p1_forensics.py` → `logs/p1_forensics_1..3.txt`; `_p1_oi_hf_self.py` → `logs/p1_oi_break.txt`; `_p1_oi_repair.py` → `logs/p1_oi_repair.txt`; `_p1_oi_boundary.py` → `logs/p1_oi_boundary.txt`. **Widened by the corrections milestone:** `_m2_volume_units.py` → `logs/m2_volume_units.txt/.json`; `_m2_lot_schedule.py` → `logs/lot_schedule.txt`; `_m2_diag{,2,3}.py` (residual-anomaly diagnostics); `_m3_oi_norm.py` → `logs/m3_oi_norm.txt/.json`; `_m4_terminal_candle.py` → `logs/m4_terminal_candle.txt/.json`; `_m5_completeness.py` → `logs/m5_completeness.txt/.json` |
| Known defects | Whole-row duplication on 6 of 18 audited files, up to **94.7%** of rows (2023-06-22), then 90.8 / 88.0 / 86.3 / 74.0 / 2.0% — dedup-safe: inside a duplicated minute prices and OI never disagree; **OI scale break pinned to 2025-01-01** — official_OI / archive_OI sits at 0.996–1.001 through 2024-12-31 and at exactly ~100.0 from 2025-01-01 onward, so archive OI must be multiplied by 100 for `trading_day >= 2025-01-01`; end-of-day OI presence becomes intermittent in 2025 (41.2–100%) and is lost for most contracts by 2026 (9.6–11.9% nonzero, 64.6–66.3% of all rows zero); **from the same 2025-01-01 date the archive's intraday range under-covers the exchange** — the archive day-max sits below the exchange `HghPric` on 15.7–44.7% of audited contracts (vs ≤1.31% in 2024) and the day-min above `LwPric` on 10.0–17.2% (vs 0%); post-close stale bars at 15:42–15:46 with `volume = 0`; index files carry `volume = 0` on every row and have **no** `open_interest` column; BANKNIFTY weekly expiries absent for 2024-01…2024-11; NIFTY missing 4 expiries inside its own span (2025-10-20, 2026-06-16/23/30); newest NIFTY file (`2026-08-04`) truncated and containing no rows for its own expiry date |
| Trust for | 1-minute OHLC of traded contracts (close agrees with the exchange's `LastPric` 94.4–100% of contracts per audited day, **100% exact for every day from 2025-01-30**; open/high/low exact ≥98.7% in 2024 but degraded after 2025); **volume in share-equivalents** (contracts × that contract's lot; verified exact on lot-65 and mixed 65/75 file-days and median-exact on lot-75, `logs/m2_volume_units.txt`); open interest in share-equivalents **until 2024-12-31** and in one hundredth of that **from 2025-01-01** (`logs/m3_oi_norm.txt`); contract existence; minute labels are **open time**, a bar stamped `T` covering `[T, T+1)`, with `15:30` a closing print rather than a candle (`logs/m4_terminal_candle.txt`) |
| Do not trust for | Open interest as one continuous series, or as an intraday maximum (peak archive OI matches the official EOD figure on only 18.20% of contract-days); any statement about complete coverage; rupee turnover, which this archive does not provide |

## 2. NSE archive — `BhavCopy_NSE_FO_*.csv.zip` (official daily F&O bhavcopy)

| | |
|---|---|
| Role | Official reference/reconciliation source; also the cheapest trading-day and listed-expiry arbiter |
| URL pattern | `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip` |
| Publisher | NSE (authoritative for NSE-listed derivatives) |
| Licence / terms | Exchange public archive for market reporting; free, no account, no key. Used for verification, redistributed nowhere |
| Reachability measured | `2024-01-02 → 2026-11-03`. **Nothing earlier is served** — 2021-08-31 was tried and returns 404 |
| Format observed | ZIP → single CSV, ISO-20022 style headers: `TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb, SctySrs, XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp, FinInstrmNm, OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, UndrlygPric, SttlmPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd, SsnId, NewBrdLotQty, Rmks, Rsvd1-4` |
| Identity convention | NIFTY index options = `TckrSymb == 'NIFTY' & FinInstrmTp == 'IDO'` (never `OPTIDX`, which is the legacy symbol-series code) |
| Free HTTP trick | `Range: bytes=0-3` + checking the `PK` magic answers "was this a trading day" in 4 bytes; a 404 answers "market holiday" |
| Status | **Used and verified.** 318 bhavcopies cached under `data/raw/p1_nse/` (20240101 .. 20261103), which is what let the M2/M3/M5 reconciliations widen from 21 dates to the whole free archive at no download cost |
| Evidence | `scripts/_p1_nse_bhav_inspect.py`, `_p1_nse_reconcile.py` → `logs/p1_nse_reconciliation.txt/.json`, `_p1_nse_listed_expiries.py`, `_p1_nse_expiry_coverage.py`, `_p1_oi_hf_self.py`, `_p1_tick_grid.py`, `_p1_tick_change.py`, `_p1_reference_data.py`; memoised probe cache `data/raw/p1_nse/nse_trading_day_probe.json` |
| Limits (must not be forgotten) | Daily aggregates only — it can validate a contract's day-level open/high/low/last/volume/OI, and **can never validate an individual 1-minute candle**. `LastPric` is the day's last trade and is the only column comparable with an archive close; `ClsPric` agrees with it on just 3.89–43.04% of contracts, so it is *not* a candle close, and `SttlmPric` is a derived settlement value (median gap to an option close ≈ the whole underlying) which must never be compared with a candle. Units, measured against the archive and never assumed: `TtlTradgVol` is in **contracts**, `OpnIntrst` is in **share-equivalents** (corrected 2026-11-05 — an earlier row here said *contracts*, which is what made the archive look 100× wrong on one side of 2025-01-01 only), `TtlTrfVal` is a notional/strike-based figure (median ratio 0.0016 to the archive's own rupee value) and therefore **cannot arbitrate any unit**, and `NewBrdLotQty` is the authoritative per-contract-row lot |

## 3. NSE circulars and contract-specs pages (official, textual)

| | |
|---|---|
| Role | Historical reference data: expiry weekday/cadence regimes, lot sizes, strike intervals, tick sizes |
| Items actually retrieved | `FAOP/54334` (07-Nov-2022, weekly-contract count 7→4 from 09-Dec-2022, **weekday unchanged**), `FAOP/64506` (10-Oct-2024, one benchmark index per exchange with weeklies from 20-Nov-2024; BANKNIFTY/FINNIFTY/MIDCPNIFTY weeklies discontinued), `FAOP/68747` (25-Jun-2025, NIFTY weekly **and** monthly move to Tuesday from 01-Sep-2025, with the holiday-shift rule written into the annexure) |
| Specs page | `https://www.nseindia.com/resources/downloads/contract-list-for-derivatives` / contract-specs content — price-step table recovered verbatim: **0–15,000 → ₹0.05; >15,000–30,000 → ₹0.10; >30,000 → ₹0.20**, circular `NSE/FAOP/67135` |
| Measured contradiction | That slab would put NIFTY *option premiums* on ₹0.10 for the whole audited history (index 21,352–25,986). Observed instead: official **traded** prices 0.00% off the ₹0.05 grid on every date through 2026-08-10, the HF archive 100.00% on-grid, and Breeze's own master `TickSize = 5` on 3,388 of 3,391 NIFTY rows. **Adopted: ₹0.05 for premiums**; the slab is rule text and lost to two independent observations. Separately, from **2026-08-03** the *derived* columns (`PrvsClsgPric`, `SttlmPric`) go 20.14–23.43% off-grid while traded columns stay on it — a settlement-computation change at NSE, not a tick change (`logs/p1_tick_change.txt`) |
| Retrieval | PDFs downloaded and text-extracted with `pypdf` (audit-only, listed in `requirements-audit.txt`, never imported by `nifty_quant/`; free library, not a data purchase). **Note:** the circulars give cadence and tick rules but never a time series of lot sizes — the per-row `NewBrdLotQty` in the bhavcopy is the only historical lot arbiter this project trusts |
| Status | **Verified** for NSE products from 2022 onward. BSE/SENSEX regime history is **externally reported, not circular-verified here** |
| Evidence | `scripts/_p1_nse_circulars.py` → `logs/p1_nse_circulars_text.txt`, `data/raw/p1_nse/circulars/FAOP*.pdf`; `scripts/_p1_nse_specs.py` → `logs/p1_nse_specs.txt`, `data/raw/p1_nse/contract_specs.html`; `scripts/_p1_expiry_calendar.py` → `logs/p1_expiry_calendar.txt/.json` |
| Correction recorded | Three earlier regime statements in this project were written from memory and were wrong. Each was overturned by the exchange's own `XpryDt` listing. The register therefore treats the bhavcopy expiry set as the authority and circulars as the explanation, never the reverse |

## 4. Cloud Trader Pro (free historical options datasets)

| | |
|---|---|
| Role | Optional secondary/comparison source for 1-minute expired-options data |
| URL | `https://cloudtraderpro.in/free-historical-options-data/`, `/free-historical-nifty-expired-options-data/` |
| Advertised schema | `Symbol` (NSE format, e.g. `NIFTY24DEC24000CE`), `Date`, `Time` = start of the 1-min interval, `Open/High/Low/Close` float, `Volume` integer **in contracts**, `Open Interest` integer "live open positions at the end of the minute" |
| Licence stated | **CC0** — `https://creativecommons.org/publicdomain/zero/1.0/`, read from the page's machine-readable JSON-LD block. This is materially more permissive than the HF dataset |
| Delivery | No direct file link on the site; six Google Drive file ids only |
| Status | **UNVERIFIED — could not retrieve a single byte of data.** `drive.usercontent.google.com/download?...&confirm=t` and `drive.google.com/uc` both return an HTML page; reading the body shows a Google **sign-in interstitial** (`accounts.google.com/v3/signin/interstitial`), and `drive.google.com/file/d/{id}/view` returns **HTTP 401** with a login page (not a 404, so the ids are not dead). The Drive v3 `alt=media` route answers `403 The request is missing a valid API key` |
| Why it stops here | Fetching the file would require a Google account sign-in. The governing directive forbids creating new accounts, so no credential path was attempted and **no data-quality claim is made** |
| Evidence | `scripts/_p1_cloudtrader.py` → `logs/p1_cloudtrader.txt/.json`; `_p1_cloudtrader_fetch.py` → `logs/p1_cloudtrader_sample.txt/.json`; `_p1_cloudtrader_body.py` → `logs/p1_cloudtrader_body.txt`; `_p1_cloudtrader_share.py` → `logs/p1_cloudtrader_share.txt`; page HTML in `data/raw/p1_cloudtrader/` |
| Permitted use if ever obtained | CC0 as declared, but the declared licence is itself unverified because the file was never seen. Do not cite it as usable |

## 5. ICICI Breeze Trading API

| | |
|---|---|
| Role | Broker-side historical data, potentially the only source that can re-derive post-2025 OI |
| Docs consulted | `https://api.icicidirect.com/breezeapi/documents/index.html` (saved as `data/raw/p1_breeze_api_reference.md`, 6,696 lines) and the official SDK README `https://github.com/Idirect-Tech/Breeze-Python-SDK` (saved as `data/raw/p1_breeze_sdk_readme.md`) |
| Documented endpoints | v1 `POST /api/v1/historicalcharts`; **v2 `GET https://breezeapi.icicidirect.com/api/v2/historicalcharts`** with `interval, from_date, to_date, stock_code, exchange_code, product_type, expiry_date, right, strike_price` (all marked mandatory) |
| Documented intervals | v2 `1minute`, `5minute`, `30minute`, `1day`. **There is no documented 15-minute interval** — 15 m must be resampled. The streaming convention lists `1SEC,1MIN,5MIN,30MIN`, so 1-second is available too |
| Documented fields | Streaming/response convention for `NFO`, `BFO`, `MCX`: `exchange code, stock code, expiry date, strike price, right type, open, high, low, close, volume, oi, datetime, interval`. The reference's own NIFTY-option minute sample returns `datetime, stock_code, exchange_code, product_type, expiry_date, right, strike_price, open, high, low, close, volume, open_interest, count` — `open_interest` populated for options, `None` for cash equity |
| Documented limits | 100 API calls/minute, 5,000 API calls/day; order actions capped at 10/second; market orders not permitted; orders only from a registered static IP |
| Authentication | AppKey + SecretKey + SessionToken; session token from `GET /api/v1/customerdetails`. Requires an ICICI Direct account with API enabled — **no credentials exist in this project and none were created** |
| What was verified without credentials | The **public security master** `https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip` (2,386,297 B, five members; `FONSEScripMaster.txt` parsed, 83,568 rows, 69 columns). For `ShortName == NIFTY` it lists **3,391 rows over exactly 9 expiry dates spanning 2026-11-09 → 2027-06-29, i.e. zero already-expired contracts**, with `LotSize = 65` on all 3,391 rows and `TickSize = 5` on 3,388 of them (the 3 futures rows carry `TickSize = 10`, consistent with the price-band rule because NIFTY futures trade in the >15,000–30,000 band, while option premiums sit below 15,000) |
| Status | **UNVERIFIED as a data source.** No API response was received, no response was mocked, and nothing is claimed to work. The one artifact that could be inspected says the master file carries **only live listings**, so addressing a historical expired contract depends on the `expiry_date`+`strike`+`right` parameter route working for delisted contracts — which is asserted nowhere in the docs and cannot be tested without an account |
| Evidence | `scripts/_p1_breeze_master.py` → `logs/p1_breeze_master.txt`, `data/raw/p1_breeze/security_master.parquet`; `scripts/_p1_breeze_link.py` → `logs/p1_breeze_link.txt` |
| Condition to revisit | Only if the user supplies existing Breeze credentials. Then the first test must be a real expired-contract minute query, and a `200` with an empty `Success` list must be recorded as a failure, not a success |

## 6. Sources deliberately not used

| Source | Reason |
|---|---|
| Upstox / Dhan / Kotak / 5paisa APIs | Each needs a new account or a paid plan. The directive allows neither |
| NSE live Option Chain / `nseIndices` JSON APIs | Current-state only, no history — that alone disqualifies them for backtest data. The working calls used `curl_cffi` with `impersonate="chrome124"` plus a warm-up GET on `www.nseindia.com`; **no control test was run** to prove a plain client is rejected there, so "warm-up required" is convention, not a P1 measurement (the only observed 403 was the BSE quote API) |
| yfinance | Already the project's equity provider. **Not tested for NSE index options in P1** — `NSE/NIFTY` option chains were never queried, so no claim is made either way; its free 1m/5m history is short (~7/~60 days) which is the reason it cannot carry 2021-onwards intraday work |
| Paid vendors (TradeTAJI, DataFeed, Global Datafeeds, InstTicks, etc.) | Budget is ₹0 |
| Brokers' "expired options data" blog walkthroughs (e.g. tradehull) | Third-party instructions, not data; no capability claim made from them |

## Provenance summary

| Source | Licence | Commercial use | Raw data in repo | Verified by inspection |
|---|---|---|---|---|
| HF `india-index-options-1m` | CC-BY-NC-4.0 | **No** | No (`data/raw/` ignored) | Yes |
| NSE bhavcopy | Exchange public archive | n/a (reference only) | No | Yes |
| NSE circulars / specs | Exchange public circulars | n/a | No | Yes |
| Cloud Trader Pro | CC0 (declared) | Declared yes | **No file obtained** | **No** |
| ICICI Breeze | Broker API, account-bound | n/a | Only the public security master | **No (no credentials)** |

## P2 free-source assessment and cost-rate sourcing (2026-11-05)

`scripts/_p2_alt_sources.py` probed, anonymously and at ₹0, whether any legally-usable free
source can extend NIFTY options **intraday** history beyond the cached archive (`logs/p2_alt_sources.txt`,
`logs/p2_coverage.txt`):

| Candidate source | Reachable anonymously? | Usable here? | Why |
|---|---|---|---|
| NSE official bhavcopy archive | Yes | Reference only | End-of-day only — no minute bars; already the daily completeness arbiter |
| Breeze / ICICI Direct API | No (sign-in) | **No** | Funded account + API keys — outside the no-accounts / ₹0 rule |
| CloudTrader / dashboard exports | No (sign-in wall) | **No** | Anonymous reachability yields no data |
| Vendor tick databases (truebias, etc.) | Commercial | **No** | Paid licence — out of scope |

**Fail-closed conclusion:** the maximum legally-cacheable intraday coverage is the current archive
(horizon 2026-08-04, newest expiry itself a truncated tail). Dates/contracts outside it stay
reported `UNKNOWN` / `MISSING` and left unavailable — never extended, back-filled or substituted.

**Derivative charge rates** (`data/reference/options_charge_schedule.csv`, `scripts/_p2_cost_schedule_audit.py`,
`logs/p2_options_cost.txt`): **P2.1** replaced the earlier fabricated `Rs 20 per lot` exchange fee with
charges on *premium turnover*, each rate read at ₹0 from an official document (fetched by
`scripts/_p21_charge_sources.py` into `data/raw/p21_charges`; verbatim excerpts in `logs/p21_excerpts_*.txt`):
NSE circular **FA73061** (wef 2026-03-01 — Equity Options on premium Rs 3,552.99/cr transaction charge +
Rs 0.01/cr NSE IPFT, each side; total unchanged at Rs 3,553/cr), **FA56129** (wef 2023-04-01 — slab rates
per lakh of premium and the IPFT recalibration Rs 0.01→Rs 50/cr), the **Finance (No.2) Bill 2024 cl.155**
(option-sale STT 0.0625% of premium → 0.10% wef 2024-10-01) and the **NSE STT / levies / stamp pages**
(STT 0.15% wef 2026-04-01 and 0.10% upto 2026-03-31, seller on premium; exercised-option STT purchaser on
intrinsic value; SEBI turnover fee 0.0001% = Rs 10/cr; stamp duty Derivatives–Equity Options 0.003% buyer).
**11 of 15 rows are `verified`** and every *currently in-force* core rate is sourced; the 3 pre-2026
`exchange_txn` rows stay `estimated` (FA56129 first-marginal-slab convention — a member's slab is unknowable
for a research book — and the flat Rs 3,503/cr era's effective date is *inferred* because NSE/FA/64323's PDF
returned 404 at ₹0), plus the GST config knob. So the `transaction_costs_verified` readiness gate stays
fail-closed for *full-horizon* strict validation, while trades dated on/after 2026-03-01 are strict-verifiable
per-trade (brokerage excluded or verified). The pre-P2.1 schedule is preserved verbatim in
`data/reference/options_charge_schedule_superseded.csv` for auditability.

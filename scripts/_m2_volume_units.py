"""Milestone-2 item 1 - verify the volume-unit relationship contract-day by contract-day.

Question
--------
The Hugging Face archive reports a per-minute ``volume`` with no unit documentation.
P1 measured that ``sum(volume) / official TtlTradgVol`` equals the board lot, which
implies the archive counts **share-equivalents (premium units x lot)** while NSE
counts **contracts**.  This script tests that claim properly instead of resting on a
median:

1. across every cached official date that overlaps a locally cached expiry file, and
2. across every board-lot regime (50 / 25 / 75 / 65), on both sides of each change,
   including the dates where two lots are official at once (the new lot applies only to
   newly listed expiries), which is why the test uses each contract row's own
   ``NewBrdLotQty`` instead of one lot per date,
3. hypothesis: ``hf_volume == official_contracts * that row's lot`` (share-equivalent).
   A second candidate - rupee turnover, ``archive value ~= official TtlTrfVal`` - was
   measured and **rejected as an arbiter**: for option rows ``TtlTrfVal`` is a
   notional/strike-based figure (implied 21,000-26,000 per share equivalent near the
   strike), not premium x shares, so it cannot distinguish the two units.  It is still
   printed, labelled as not an arbiter, so the rejection stays auditable.
4. granularity: is every ``hf_volume`` bar a whole multiple of the lot?  Contracts cannot
   trade fractional lots, so a share-equivalent series must be divisible by that day's
   lot - and where it is not, the residuals and their minute labels are counted.
5. direction: ``hf_volume > official * lot`` is impossible for a subset of the exchange's
   trades and is reported as such; ``hf_volume < official * lot`` on a contract the
   exchange traded means the archive missed trades (a completeness defect, not a unit
   defect) and is counted separately.

Nothing is written back to the raw files.  Every number here is computed from
``data/raw/`` and lands in ``logs/m2_volume_units.{txt,json}``.

Usage (from the repo root):
    python scripts/_m2_volume_units.py
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _p1_hf_schema import fetch  # noqa: E402  (anonymous resolve/main downloader)
from _p1_nse_reconcile import ensure_bhavcopy, load_official  # noqa: E402

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT_TXT = Path("logs/m2_volume_units.txt")
OUT_JSON = Path("logs/m2_volume_units.json")
KEYS = ["symbol", "expiry", "strike", "option_type", "timestamp"]

# Expiry files that are not cached locally yet but are needed to bracket each board-lot
# change (the regime boundaries are where a hardcoded lot size would be caught).  The
# boundaries quoted here are the ones MEASURED from the official per-row lot, not the
# ones assumed in P1: lot 50 from 2024-01-02, lot 25 from 2024-04-30, lot 75 from
# 2025-02-03, lot 65 from 2026-01-05.  The NIFTY lot-75 change therefore lands in the
# middle of the 2024-12/2025-01 files that are already cached, and 2024-12-31 is kept in
# the list because the provider returns 404 for it - recorded here as a missing file.
EXTRA_EXPIRIES = [
    "2024-04-25",   # last lot-50 expiry week
    "2024-05-30",   # first lot-25 expiry week
    "2024-11-28",   # last lot-25 expiry week
    "2024-12-31",   # expected first 2025 expiry week (provider 404 - see missing-file report)
    "2025-02-25",   # first expiry week fully inside the lot-75 regime
    "2025-10-28",   # last lot-75 expiry week cached
    "2025-11-25",   # first lot-65 announcement-window week (lot change measured 2026-01-05)
]

# Official dates to attempt around each boundary (one either side of every lot change).
EXTRA_DATES = [
    "20240430", "20240502", "20240503", "20240507",
    "20241127", "20241128", "20241202",
    "20251030", "20251031", "20251103", "20251104",
    "20240222", "20240327", "20240627", "20240829", "20240926", "20241024",
    "20250227", "20250424", "20250529", "20250626", "20250724", "20250821",
    "20251224", "20260122", "20260325", "20260423", "20260528", "20260625", "20260722",
]


def local_nifty_expiries() -> list[str]:
    out = []
    for path in sorted(HF.glob("options__NIFTY__*.parquet")):
        out.append(path.stem.replace("options__NIFTY__", ""))
    return out


def load_hf_days(expiry: str) -> tuple[pd.DataFrame, int, int]:
    """Return (deduped rows, raw row count, duplicate row count) for one expiry file."""
    path = HF / f"options__NIFTY__{expiry}.parquet"
    if not path.exists():
        path = fetch(f"options/NIFTY/{expiry}.parquet")
    raw = pd.read_parquet(path)
    n_raw = len(raw)
    dup = int(raw.duplicated(subset=KEYS).sum())
    frame = raw.drop_duplicates(subset=KEYS, keep="first").copy()
    frame["strike"] = frame["strike"].astype("int64")
    return frame, n_raw, dup

def gcd_of(frame: pd.DataFrame, column: str) -> int | None:
    values = [int(v) for v in frame[column].dropna().unique() if v > 0]
    if not values:
        return None
    return int(math.gcd(*values))


def compare_pair(day_iso: str, expiry: str, official: pd.DataFrame, hf: pd.DataFrame) -> dict | None:
    """Compare one file-day of archive volume against the official traded volume.

    The comparison is made *per contract row* using that row's own ``NewBrdLotQty``,
    because NSE applies the new lot only to newly listed expiries and several dates
    carry two different lots at once.  A single lot per date would make those dates
    untestable, and testing them is the whole point of the exercise.
    """
    hf_day = hf[hf["trading_day"] == day_iso].copy()
    if hf_day.empty:
        return None
    lots = sorted(set(pd.to_numeric(official["NewBrdLotQty"], errors="coerce")
                      .dropna().astype("int64")))

    agg = hf_day.groupby(["strike", "option_type"], observed=True, sort=False).agg(
        hf_volume=("volume", "sum"),
        hf_bars=("volume", "size"),
    ).reset_index()

    off = official[official["expiry"] == expiry].copy()
    off["row_lot"] = pd.to_numeric(off["NewBrdLotQty"], errors="coerce")
    off = off[off["row_lot"].notna() & (off["row_lot"] > 0)]
    off = off[["strike", "option_type", "row_lot", "TtlTradgVol", "TtlTrfVal",
               "OpnIntrst", "LastPric"]]
    merged = agg.merge(off, on=["strike", "option_type"], how="outer", indicator=True)
    both = merged[merged["_merge"] == "both"].copy()

    result: dict = {
        "date": day_iso,
        "expiry_file": expiry,
        "official_lot_sizes_on_date": lots,
        "n_lot_sizes": len(lots),
        "contracts_matched": int(len(both)),
        "hf_only": int((merged["_merge"] == "left_only").sum()),
        "official_only": int((merged["_merge"] == "right_only").sum()),
        "hf_rows_on_day": int(len(hf_day)),
    }
    if both.empty:
        result["usable"] = False
        result["why"] = "no contract identity in common between archive and official report"
        return result

    traded = both[pd.to_numeric(both["TtlTradgVol"], errors="coerce") > 0].copy()
    result["contracts_official_traded"] = int(len(traded))
    if traded.empty:
        result["usable"] = False
        result["why"] = "official report lists no traded contract for this expiry"
        return result

    traded["contracts"] = pd.to_numeric(traded["TtlTradgVol"], errors="coerce").astype("int64")
    traded["hf_volume"] = pd.to_numeric(traded["hf_volume"], errors="coerce").astype("int64")
    row_lot = traded["row_lot"].astype("int64")
    expected = traded["contracts"] * row_lot
    share_ratio = traded["hf_volume"] / expected

    result["h_share_equivalent_exact_pct"] = round(100.0 * float((traded["hf_volume"] == expected).mean()), 2)
    result["h_share_equivalent_within_1pct_pct"] = round(100.0 * float(
        ((traded["hf_volume"] <= expected * 1.01) & (traded["hf_volume"] >= expected * 0.99)).mean()), 2)
    result["row_ratio_median"] = round(float(share_ratio.median()), 5)
    result["row_ratio_p10"] = round(float(share_ratio.quantile(0.10)), 5)
    result["row_ratio_p90"] = round(float(share_ratio.quantile(0.90)), 5)
    # hf volume per official contract, which should equal that row's lot.
    per_contract = traded["hf_volume"] / traded["contracts"]
    result["hf_shares_per_official_contract"] = round(float((per_contract / row_lot).median()), 5)
    result["sum_abs_delta_shares"] = int((traded["hf_volume"] - expected).abs().sum())
    result["impossible_archive_exceeds_official"] = int((traded["hf_volume"] > expected * 1.01).sum())
    result["missed_archive_below_official"] = int((traded["hf_volume"] < expected * 0.99).sum())
    result["archive_zero_volume_official_traded"] = int((traded["hf_volume"] == 0).sum())
    result["max_upward_delta_shares"] = int((traded["hf_volume"] - expected).max())

    # Rupee turnover is recorded for completeness only: for options ``TtlTrfVal`` is a
    # notional/strike-based figure (implied ~21k-26k per share equivalent near the
    # strike), NOT premium x shares, so it cannot arbitrate a volume unit.  Measured and
    # reported, never used as evidence.
    value = (traded["hf_volume"].astype(float)
             * pd.to_numeric(traded["LastPric"], errors="coerce"))
    turn = (value / pd.to_numeric(traded["TtlTrfVal"], errors="coerce").replace(0, np.nan)).dropna()
    result["turnover_ratio_NOT_AN_ARBITER"] = (round(float(turn.median()), 4)
                                               if len(turn) else None)

    # Bar-level granularity: the day total can match while individual minute bars are not
    # whole multiples of the lot, which is what the 2024 opening-minute artifact is.
    bars = hf_day[["strike", "option_type", "volume", "timestamp"]].merge(
        off[["strike", "option_type", "row_lot"]], on=["strike", "option_type"], how="inner")
    bars["clock"] = pd.to_datetime(bars["timestamp"]).dt.strftime("%H:%M")
    bars["resid"] = bars["volume"].astype("int64") % bars["row_lot"].astype("int64")
    bad = bars[bars["resid"] != 0]
    result["bars_total"] = int(len(bars))
    result["bars_non_integral"] = int(len(bad))
    result["bars_non_integral_pct"] = round(100.0 * len(bad) / max(len(bars), 1), 3)
    result["residual_counts"] = {int(k): v for k, v in
                                 Counter(bad["resid"].tolist()).most_common(6)}
    result["residual_clocks"] = dict(Counter(bad["clock"].tolist()).most_common(5))

    idle = both[pd.to_numeric(both["TtlTradgVol"], errors="coerce").fillna(0) == 0]
    result["contracts_official_not_traded"] = int(len(idle))
    if len(idle):
        result["idle_archive_volume_gt0"] = int(
            (pd.to_numeric(idle["hf_volume"], errors="coerce") > 0).sum())
    result["usable"] = True
    return result


def main() -> int:
    logs: list = []
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say("M2 item 1 - volume unit verification")
    say("=" * 78)

    for expiry in EXTRA_EXPIRIES:
        try:
            fetch(f"options/NIFTY/{expiry}.parquet")
        except Exception as exc:  # noqa: BLE001
            say(f"  could not fetch HF expiry file {expiry}: {type(exc).__name__}: {exc}")
    for day in EXTRA_DATES:
        ensure_bhavcopy(day)

    expiries = local_nifty_expiries()
    say(f"local NIFTY expiry files: {len(expiries)}")

    day_list = sorted({p.stem[-8:] for p in NSE.glob("BhavCopy_NSE_FO_*.zip")})
    say(f"cached official dates: {len(day_list)} ({day_list[0]} .. {day_list[-1]})")
    cached_iso = {f"{day[:4]}-{day[4:6]}-{day[6:]}" for day in day_list}

    per_day: dict[str, list] = defaultdict(list)
    official_cache: dict[str, pd.DataFrame] = {}

    def official_for(day: str) -> pd.DataFrame | None:
        """One official bhavcopy, parsed once and kept (it is ~1.7k rows, cheap)."""
        if day in official_cache:
            return official_cache[day]
        if ensure_bhavcopy(day) is None:
            official_cache[day] = pd.DataFrame()
            return None
        try:
            frame = load_official(day)
        except Exception as exc:  # noqa: BLE001
            say(f"  {day}: official load failed {type(exc).__name__}: {exc}")
            frame = pd.DataFrame()
        official_cache[day] = frame
        return frame if not frame.empty else None

    # One archive file in memory at a time: the files are 0.2-0.7M rows each and holding
    # all of them at once is unnecessary and heavy.
    untested_days: dict[str, int] = defaultdict(int)
    for expiry in expiries:
        frame, n_raw, dup = load_hf_days(expiry)
        days_in_file = sorted(set(frame["trading_day"]))
        for day in day_list:
            iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            if iso not in days_in_file:
                continue
            official = official_for(day)
            if official is None:
                per_day[iso].append({"date": iso, "expiry_file": expiry, "usable": False,
                                     "why": "official arbiter cached but empty/unreadable"})
                continue
            try:
                row = compare_pair(iso, expiry, official, frame)
            except Exception as exc:  # noqa: BLE001
                row = {"date": iso, "expiry_file": expiry, "error": f"{type(exc).__name__}: {exc}"}
            if row:
                row["dup_rows_in_file"] = dup
                row["raw_rows_in_file"] = n_raw
                per_day[iso].append(row)
        for day in days_in_file:
            if str(day)[:10] not in cached_iso:
                untested_days[str(day)[:10]] += 1

    say("")
    say("PER DATE x EXPIRY-FILE RESULT (contracts officially traded, per-row lot)")
    say("-" * 110)
    say(f"{'date':11s} {'expiry':11s} {'lots':>9s} {'n':>4s} {'exact%':>7s} {'<=1%':>6s} "
        f"{'rMed':>7s} {'rP10':>7s} {'rP90':>7s} {'dSum':>6s} {'nonint%':>8s} "
        f"{'impos':>5s} {'miss':>5s} {'zero':>5s}")
    for iso in sorted(per_day):
        for row in per_day[iso]:
            if not row.get("usable"):
                reason = row.get("error") or row.get("why") or "not usable"
                logs.append(row)
                say(f"{iso:11s} {row.get('expiry_file', ''):11s} SKIP {reason[:80]}")
                continue
            lots = "/".join(str(x) for x in row["official_lot_sizes_on_date"])
            logs.append(row)
            say(f"{iso:11s} {row['expiry_file']:11s} {lots:>9s} "
                f"{row['contracts_official_traded']:>4d} "
                f"{row['h_share_equivalent_exact_pct']:>7.2f} "
                f"{row['h_share_equivalent_within_1pct_pct']:>6.2f} "
                f"{row['row_ratio_median']:>7.4f} {row['row_ratio_p10']:>7.4f} "
                f"{row['row_ratio_p90']:>7.4f} {row['sum_abs_delta_shares']:>6d} "
                f"{row['bars_non_integral_pct']:>8.3f} "
                f"{row['impossible_archive_exceeds_official']:>5d} "
                f"{row['missed_archive_below_official']:>5d} "
                f"{row['archive_zero_volume_official_traded']:>5d}")

    say("")
    say("ARCHIVE DAYS WITH NO CACHED OFFICIAL ARBITER (not downloaded on this run)")
    say("-" * 110)
    before_floor = [d for d in untested_days if d < "2024-01-02"]
    say(f"  {len(untested_days)} distinct archive days untested: "
        f"{len(before_floor)} before the official F&O archive floor (2024-01-02), "
        f"{len(untested_days) - len(before_floor)} after it")
    say(f"  days after the floor: {sorted(d for d in untested_days if d >= '2024-01-02')[:20]}")

    say("")
    say("REGIME SUMMARY (grouped by the lot sizes official on that date)")
    say("-" * 110)
    by_lot = defaultdict(list)
    for row in logs:
        if row.get("usable"):
            by_lot[tuple(row["official_lot_sizes_on_date"])].append(row)
    for lots in sorted(by_lot):
        rows = by_lot[lots]
        dates = sorted({r["date"] for r in rows})
        contracts = sum(r["contracts_official_traded"] for r in rows)
        say(f"lots {str(lots):>10s}: {len(rows):>3d} file-days, {len(dates):>2d} distinct dates "
            f"({dates[0]} .. {dates[-1]}), {contracts:>6d} traded contracts, "
            f"median exact {np.median([r['h_share_equivalent_exact_pct'] for r in rows]):.2f}%, "
            f"median row ratio {np.median([r['row_ratio_median'] for r in rows]):.5f}, "
            f"median non-integral bars {np.median([r['bars_non_integral_pct'] for r in rows]):.3f}%, "
            f"sum|delta| {sum(r['sum_abs_delta_shares'] for r in rows):d} shares")

    say("")
    say("LOT SCHEDULE PER OFFICIAL DATE (per-row NewBrdLotQty over all NIFTY options)")
    say("-" * 110)
    series: dict[str, tuple[tuple[int, ...], int]] = {}
    for day in day_list:
        iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        official = official_for(day)
        if official is None:
            continue
        numeric = pd.to_numeric(official["NewBrdLotQty"], errors="coerce").dropna().astype("int64")
        if numeric.empty:
            continue
        series[iso] = (tuple(sorted(set(numeric))), int(numeric.value_counts().idxmax()))
    prev = None
    mixed = [iso for iso, (lots, _modal) in series.items() if len(lots) > 1]
    for iso in sorted(series):
        lots, modal = series[iso]
        if prev is None or modal != prev:
            say(f"  {iso}: lot -> {modal}  (distinct on date: {list(lots)})")
        prev = modal
    say(f"  distinct NIFTY lot values seen: "
        f"{sorted({l for lots, _m in series.values() for l in lots})} over {len(series)} official dates")
    say(f"  dates carrying MORE THAN ONE lot (new lot applies only to newly listed expiries): "
        f"{len(mixed)} -> {mixed[:12]}")

    say("")
    say("PRE-OFFICIAL-ERA GRANULARITY (2021-2023: no official arbiter, self-consistency only)")
    say("-" * 78)
    for expiry in expiries:
        if expiry >= "2024-01-01":
            continue
        frame, _n_raw, _dup = load_hf_days(expiry)
        for day in sorted(set(frame["trading_day"]))[:3]:
            sub = frame[frame["trading_day"] == day]
            g = gcd_of(sub, "volume")
            say(f"  {day} ({expiry}): gcd(volume)={g}, n_volume_values={sub['volume'].nunique()}, "
                f"min>0={int(sub.loc[sub['volume'] > 0, 'volume'].min()) if (sub['volume'] > 0).any() else 0}")

    say("")
    say("VERDICT")
    say("-" * 110)
    usable = [r for r in logs if r.get("usable")]
    if usable:
        exact = np.mean([r["h_share_equivalent_exact_pct"] for r in usable])
        nonint = np.mean([r["bars_non_integral_pct"] for r in usable])
        impos = sum(r["impossible_archive_exceeds_official"] for r in usable)
        missed = sum(r["missed_archive_below_official"] for r in usable)
        say(f"  file-days tested: {len(usable)} over "
            f"{len({r['date'] for r in usable})} distinct dates")
        say(f"  mean exact share-equivalent match (archive = contracts x that row's lot): "
            f"{exact:.2f}%")
        say(f"  mean share of minute bars whose volume is NOT a whole multiple of the lot: "
            f"{nonint:.3f}%")
        say(f"  archive ABOVE official x lot beyond 1% tolerance: {impos} contract-days")
        say(f"  archive BELOW official x lot beyond 1% tolerance (missed volume): {missed} contract-days")
        say(f"  summed absolute share difference across all tested contract-days: "
            f"{sum(r['sum_abs_delta_shares'] for r in usable):,}")
        say(f"  rupee-turnover ratio (recorded only, NOT an arbiter for options): "
            f"median {np.median([r['turnover_ratio_NOT_AN_ARBITER'] for r in usable if r['turnover_ratio_NOT_AN_ARBITER']]):.4f}")
        say("  unit conclusion: provider volume is share-equivalents = contracts x board "
            "lot; converting to contracts divides by that contract's own lot on that day")
    OUT_JSON.write_text(json.dumps(logs, indent=2, default=str), encoding="utf-8")
    say(f"wrote {OUT_TXT} and {OUT_JSON} ({len(logs)} file-day records)")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

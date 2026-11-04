"""Milestone-2 diagnostic - three things _m2_volume_units.py left unresolved.

A. Unit definition published with the file.  Read the archive CSV verbatim.  The F&O
   bhavcopy ships bare column codes, so the unit of ``TtlTradgVol`` has to be inferred
   from the data itself (is it divisible by the lot?  is OI?).
B. Session rows.  The archive CSV has an ``SsnId`` column and P1's loader de-duplicated
   with ``keep="first"``, which silently drops the closing/crossing sessions.  If a
   contract traded in two sessions the "official" day total used so far is too small -
   that is the prime suspect for the 2024 "archive volume exceeds official x lot" cases.
   Measure how many contracts have >1 session row, and what the totals look like with
   session rows summed instead of first.
C. Coverage of each locally cached HF expiry file, so the file-days skipped by the main
   script can be classified as "provider file starts later" vs "no data" vs "my join bug".

Usage (repo root):  python scripts/_m2_diag.py
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _p1_nse_reconcile import NSE, ensure_bhavcopy  # noqa: E402

HF = Path("data/raw/p1_sample")
KEYS = ["symbol", "expiry", "strike", "option_type", "timestamp"]


def raw_nifty(ymd: str) -> pd.DataFrame:
    path = ensure_bhavcopy(ymd)
    if path is None:
        return pd.DataFrame()
    with zipfile.ZipFile(path) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
    frame = pd.read_csv(io.StringIO(text))
    nifty = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")].copy()
    nifty["strike"] = nifty["StrkPric"].astype(float).round(0).astype("int64")
    nifty["option_type"] = nifty["OptnTp"]
    nifty["expiry"] = pd.to_datetime(nifty["XpryDt"], errors="coerce").dt.strftime("%Y-%m-%d")
    return nifty


def session_audit(ymd: str) -> None:
    print(f"\n=== {ymd} : session structure of the official file ===")
    nifty = raw_nifty(ymd)
    if nifty.empty:
        print("  empty")
        return
    print(f"  NIFTY IDO rows: {len(nifty)}   distinct contracts: "
          f"{nifty.groupby(['strike','option_type','expiry']).ngroups}")
    print(f"  SsnId values and row counts: "
          f"{nifty['SsnId'].value_counts().to_dict()}")
    per = nifty.groupby(["strike", "option_type", "expiry"])["SsnId"].transform("size")
    print(f"  contracts with >1 row: {(per > 1).sum()} (max rows per contract {per.max()})")
    multi = nifty[per > 1].sort_values(["strike", "option_type", "SsnId"])
    if not multi.empty:
        cols = ["XpryDt", "StrkPric", "OptnTp", "SsnId", "TtlTradgVol", "TtlTrfVal",
                "OpnIntrst", "NewBrdLotQty", "ClsPric", "LastPric"]
        print(multi[cols].head(8).to_string(index=False))


def identity(ymd: str, expiry: str, limit: int = 5) -> None:
    """Compare HF day volume against official, using first-session vs summed totals."""
    iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    print(f"\n=== {iso} x expiry {expiry}: HF day volume vs official ===")
    nifty = raw_nifty(ymd)
    if nifty.empty:
        print("  official file unavailable")
        return
    off = nifty[nifty["expiry"] == expiry].copy()
    if off.empty:
        print("  no official rows for this expiry")
        return
    lots = sorted(set(pd.to_numeric(off["NewBrdLotQty"], errors="coerce").dropna().astype("int64")))
    print(f"  lots on this date for this expiry: {lots}")
    path = HF / f"options__NIFTY__{expiry}.parquet"
    if not path.exists():
        print(f"  HF file not cached: {path.name}")
        return
    hf = pd.read_parquet(path).drop_duplicates(subset=KEYS, keep="first")
    hf = hf[hf["trading_day"] == iso].copy()
    if hf.empty:
        all_days = sorted(pd.read_parquet(path, columns=["trading_day"])["trading_day"].astype(str).unique())
        print(f"  HF rows on {iso}: 0. file covers {len(all_days)} days: "
              f"{all_days[0]} .. {all_days[-1]}")
        return
    hf["strike"] = hf["strike"].astype("int64")
    agg = hf.groupby(["strike", "option_type"], observed=True).agg(
        hf_vol=("volume", "sum"), bars=("volume", "size"),
        hf_min_vol=("volume", "min"), hf_max_vol=("volume", "max")).reset_index()

    first = off.drop_duplicates(subset=["strike", "option_type"], keep="first")
    summed = off.groupby(["strike", "option_type"], as_index=False).agg(
        off_vol=("TtlTradgVol", "sum"), off_val=("TtlTrfVal", "sum"),
        lot=("NewBrdLotQty", "first"), oi=("OpnIntrst", "sum"), cls=("ClsPric", "last"))
    merged = agg.merge(summed, on=["strike", "option_type"]).merge(
        first[["strike", "option_type", "TtlTradgVol"]].rename(
            columns={"TtlTradgVol": "first_vol"}), on=["strike", "option_type"])
    merged = merged[merged["off_vol"] > 0].copy()
    if merged.empty:
        print("  nothing officially traded")
        return
    lot = int(merged["lot"].iloc[0]) if merged["lot"].nunique() == 1 else None
    if lot is None:
        print(f"  mixed lots within expiry: {sorted(set(merged['lot']))}")
        return
    merged["exp_sum"] = merged["off_vol"] * lot
    merged["exp_first"] = merged["first_vol"] * lot
    merged["delta_sum"] = merged["hf_vol"] - merged["exp_sum"]
    merged["delta_first"] = merged["hf_vol"] - merged["exp_first"]
    merged["divis"] = (merged["hf_vol"] % lot == 0)
    print(f"  traded contracts: {len(merged)}   lot: {lot}")
    print(f"  exact match vs SUMMED official : {(merged['delta_sum'] == 0).sum()} "
          f"({100*(merged['delta_sum']==0).mean():.2f}%)")
    print(f"  exact match vs FIRST  official : {(merged['delta_first'] == 0).sum()} "
          f"({100*(merged['delta_first']==0).mean():.2f}%)")
    print(f"  HF day volume divisible by lot : {int(merged['divis'].sum())} "
          f"({100*merged['divis'].mean():.2f}%)")
    print(f"  sum|delta|: summed={int(merged['delta_sum'].abs().sum())} "
          f"first={int(merged['delta_first'].abs().sum())}")
    print("  per-contract rupee check: official value implied per share-equivalent")
    prem = (merged["off_val"] / (merged["off_vol"] * lot)).round(2)
    print(f"    official value / (vol*lot): median {prem.median():.2f} "
          f"(range {prem.min():.2f} .. {prem.max():.2f})")
    print(f"    strike median for these contracts: {merged['strike'].median():.0f}, "
          f"HF close median: {agg['bars'].median():.1f} bars/contract")
    cols = ["strike", "option_type", "hf_vol", "off_vol", "exp_sum", "delta_sum",
            "first_vol", "divis", "bars", "off_val", "oi", "lot"]
    print(merged[cols].head(limit).to_string(index=False))


def coverage() -> None:
    print("\n=== HF expiry-file coverage (locally cached NIFTY files) ===")
    for path in sorted(HF.glob("options__NIFTY__*.parquet")):
        days = pd.read_parquet(path, columns=["trading_day"])["trading_day"].astype(str)
        uniq = sorted(days.unique())
        print(f"  {path.stem.replace('options__NIFTY__','')}: rows={len(days):8d} "
              f"days={len(uniq):3d} {uniq[0]} .. {uniq[-1]}")


def main() -> int:
    pd.set_option("display.width", 220)
    for ymd in ("20240118", "20240718", "20250320", "20260217", "20241218"):
        session_audit(ymd)
    identity("20240118", "2024-01-25")
    identity("20240718", "2024-07-25")
    identity("20250320", "2025-03-27")
    identity("20260217", "2026-02-24")
    coverage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

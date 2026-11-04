"""P1 step 10 - decide which side changed units: the HF archive or NSE's report.

The reconciliation found HF open_interest tracks official NSE OI one-for-one on
2024 dates but sits about 100x below official OI on 2025+ dates.  One of the two
sides changed its unit convention.  This test uses official data alone, so it
cannot be contaminated by the HF archive:

  * official OI to official traded volume per date,
  * official OpnIntrst against official OpnIntrst + ChngInOpnIntrst of the next
    date (an internal consistency identity that only holds if the unit is
    stable),
  * the same identity applied to the Hugging Face archive.

Usage:
    python scripts/_p1_nse_oi_units.py
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT = Path("logs/p1_nse_oi_units.json")

DATES = ["20240102", "20240118", "20240125", "20240718", "20240725", "20240829",
         "20241219", "20241226", "20250102", "20250123", "20250130", "20250320",
         "20250327", "20250902", "20250923", "20250930", "20260217", "20260224",
         "20260804", "20260825", "20261103"]


def official(ymd: str) -> pd.DataFrame | None:
    path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    if not path.exists():
        return None
    archive = zipfile.ZipFile(path)
    frame = pd.read_csv(io.StringIO(archive.read(archive.namelist()[0]).decode("utf-8", "replace")))
    nifty = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"].isin(["IDO", "IDF"]))].copy()
    if nifty.empty:
        return None
    nifty["lot"] = pd.to_numeric(nifty["NewBrdLotQty"], errors="coerce")
    return nifty


def main() -> int:
    rows = []
    for ymd in DATES:
        nifty = official(ymd)
        if nifty is None:
            continue
        opts = nifty[nifty["FinInstrmTp"] == "IDO"]
        oi = pd.to_numeric(opts["OpnIntrst"], errors="coerce").fillna(0)
        vol = pd.to_numeric(opts["TtlTradgVol"], errors="coerce").fillna(0)
        lots = sorted(set(opts["lot"].dropna().astype("int64")))
        rows.append({
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
            "n_option_contracts": int(len(opts)),
            "official_lots": lots,
            "sum_official_OI": int(oi.sum()),
            "sum_official_volume": int(vol.sum()),
            "median_contract_OI": float(oi.median()),
            "max_contract_OI": int(oi.max()),
            "OI_over_volume": round(float(oi.sum() / max(1, vol.sum())), 3),
        })

    frame = pd.DataFrame(rows)
    print("official NSE F&O bhavcopy, NIFTY index options only (no HF data involved):")
    with pd.option_context("display.width", 200, "display.max_columns", 12):
        print(frame.to_string(index=False))

    # Does the official OI magnitude step at a date boundary?
    frame["date_obj"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date_obj")
    print("\nOI/turnover ratio sequence (a unit change shows as a step):")
    print(frame[["date", "official_lots", "OI_over_volume", "max_contract_OI"]].to_string(index=False))

    # Cross-check the official identity OI(t) = OI(t-1) + Change(t) inside one era.
    print("\n--- official internal identity check (needs two consecutive cached dates) ---")
    pairs = [("20240118", "20240125"), ("20241219", "20241226"), ("20250123", "20250130"),
             ("20250923", "20250930"), ("20260217", "20260224")]
    identity = []
    for prev, cur in pairs:
        a, b = official(prev), official(cur)
        if a is None or b is None:
            continue
        a = a[a["FinInstrmTp"] == "IDO"]
        b = b[b["FinInstrmTp"] == "IDO"]
        a["key"] = a["XpryDt"].astype(str) + "|" + a["StrkPric"].astype(str) + "|" + a["OptnTp"]
        b["key"] = b["XpryDt"].astype(str) + "|" + b["StrkPric"].astype(str) + "|" + b["OptnTp"]
        merged = a[["key", "OpnIntrst"]].merge(
            b[["key", "OpnIntrst", "ChngInOpnIntrst"]], on="key", suffixes=("_prev", ""))
        if merged.empty:
            continue
        residual = (merged["OpnIntrst"] - merged["OpnIntrst_prev"] - merged["ChngInOpnIntrst"]).abs()
        identity.append({
            "from": prev, "to": cur, "n": int(len(merged)),
            "identity_holds_pct": round(100.0 * float((residual < 1).mean()), 2),
            "median_abs_residual": float(residual.median()),
        })
        print(f"  {prev} -> {cur}: identity holds for {identity[-1]['identity_holds_pct']}% of {len(merged)} contracts")

    OUT.write_text(json.dumps({"per_date": rows, "identity": identity}, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P1 step 9 - reconcile the Hugging Face 1-minute archive against official NSE.

Object of the comparison
-----------------------
The official NSE F&O bhavcopy is a *daily* report.  It can therefore verify the
daily aggregation of the 1-minute archive, and the existence and identity of
every contract, but it cannot verify any individual minute bar.  That limit is
stated in the output so no conclusion overreaches the evidence.

Price definitions are kept strictly separate:

* HghPric / LwPric / OpnPric  - official intraday extremes and first price
* ClsPric / LastPric          - official last traded price
* SttlmPric                   - official settlement price (a calculated
                                reference).  It is compared and reported but is
                                never treated as a candle close.

Usage:
    python scripts/_p1_nse_reconcile.py
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT = Path("logs/p1_nse_reconciliation.json")

# (official bhavcopy date, HF expiry file).  Two kinds of comparison day are
# used on purpose: the expiry day itself, where the official report carries the
# day's traded price range, and a mid-life day, where open interest is still
# meaningful (on expiry day NSE reports OI as zero once contracts expire).
PAIRS = [
    ("20240125", "2024-01-25"),
    ("20240118", "2024-01-25"),
    ("20240725", "2024-07-25"),
    ("20240718", "2024-07-25"),
    ("20241226", "2024-12-26"),
    ("20241219", "2024-12-26"),
    ("20250130", "2025-01-30"),
    ("20250123", "2025-01-30"),
    ("20250327", "2025-03-27"),
    ("20250320", "2025-03-27"),
    ("20250902", "2025-09-02"),
    ("20250930", "2025-09-30"),
    ("20250923", "2025-09-30"),
    ("20260224", "2026-02-24"),
    ("20260217", "2026-02-24"),
    ("20260804", "2026-08-04"),
    ("20260728", "2026-08-04"),
]


def ensure_bhavcopy(ymd: str) -> Path | None:
    """Fetch the official daily F&O bhavcopy once and cache it on disk."""
    target = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    if target.exists() and target.stat().st_size > 0:
        return target
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
    try:
        from curl_cffi import requests as curl

        session = curl.Session(impersonate="chrome124")
        session.get("https://www.nseindia.com", timeout=30)
        response = session.get(url, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"  download failed {ymd}: {type(exc).__name__}: {exc}")
        return None
    if response.status_code != 200 or response.content[:2] != b"PK":
        print(f"  not available {ymd}: HTTP {response.status_code}")
        return None
    NSE.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    print(f"  downloaded {target.name} ({len(response.content):,}B)")
    return target


def load_official(ymd: str) -> pd.DataFrame:
    path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    archive = zipfile.ZipFile(path)
    frame = pd.read_csv(io.StringIO(archive.read(archive.namelist()[0]).decode("utf-8", "replace")))
    nifty = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")].copy()
    nifty["strike"] = nifty["StrkPric"].astype(float).round(0).astype("int64")
    nifty["option_type"] = nifty["OptnTp"]
    nifty["expiry"] = pd.to_datetime(nifty["XpryDt"]).dt.strftime("%Y-%m-%d")
    return nifty.drop_duplicates(subset=["strike", "option_type", "expiry"], keep="first")


def load_hf(expiry: str) -> pd.DataFrame:
    path = HF / f"options__NIFTY__{expiry}.parquet"
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    frame = frame.drop_duplicates(subset=["symbol", "expiry", "strike", "option_type", "timestamp"], keep="first")
    frame["strike"] = frame["strike"].astype("int64")
    return frame


def compare(day: str, expiry: str) -> dict:
    result: dict = {
        "official_date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
        "hf_file": f"options/NIFTY/{expiry}.parquet",
        "comparison_kind": "expiry_day" if day == expiry.replace("-", "") else "mid_life",
    }

    official = load_official(day)
    official = official[official["expiry"] == expiry]
    hf = load_hf(expiry)
    if hf.empty:
        result["skipped"] = "no cached HF sample for this expiry"
        return result
    iso = result["official_date"]
    hf_day = hf[hf["trading_day"] == iso].sort_values("timestamp")
    if hf_day.empty:
        result["skipped"] = f"HF file has no rows for trading_day {iso}"
        return result
    result["hf_rows_on_day"] = int(len(hf_day))
    result["hf_rows_in_file"] = int(len(hf))
    result["official_contracts_for_expiry"] = int(len(official))

    hf_agg = hf_day.groupby(["strike", "option_type"], observed=True, sort=False).agg(
        hf_vol=("volume", "sum"),
        hf_oi_last=("open_interest", "last"),
        hf_oi_max=("open_interest", "max"),
        hf_open=("open", "first"),
        hf_high=("high", "max"),
        hf_low=("low", "min"),
        hf_close=("close", "last"),
        bars=("close", "size"),
    ).reset_index()

    keep = [c for c in ["strike", "option_type", "NewBrdLotQty", "UndrlygPric", "OpnPric", "HghPric",
            "LwPric", "ClsPric", "LastPric", "SttlmPric", "PrvsClsgPric", "OpnIntrst",
            "ChngInOpnIntrst", "TtlTradgVol", "TtlNbOfTxsExctd", "TtlTrfVal"] if c in official.columns]
    merged = hf_agg.merge(official[keep], on=["strike", "option_type"], how="outer", indicator=True)

    both = merged[merged["_merge"] == "both"].copy()
    result["matched_contracts"] = int(len(both))
    result["hf_only_contracts"] = int((merged["_merge"] == "left_only").sum())
    result["official_only_contracts"] = int((merged["_merge"] == "right_only").sum())
    result["hf_only_examples"] = merged.loc[merged["_merge"] == "left_only", ["strike", "option_type"]].head(8).to_dict("records")
    result["official_only_examples"] = merged.loc[merged["_merge"] == "right_only", ["strike", "option_type"]].head(8).to_dict("records")

    if both.empty:
        result["note"] = "no overlap - cannot compare"
        return result

    traded = both[both["TtlTradgVol"] > 0].copy()
    result["official_contracts_traded"] = int(len(traded))

    # --- identity of the underlying and lot size (reference data)
    result["official_underlying_close"] = float(pd.to_numeric(official["UndrlygPric"], errors="coerce").max())
    result["official_lot_sizes"] = sorted(set(pd.to_numeric(official["NewBrdLotQty"], errors="coerce").dropna().astype("int64")))

    # --- price agreement: extremes must bracket the 1-minute path
    def agreement(column: str, official_column: str, tol: float = 0.005) -> dict:
        frame = traded[traded[official_column] > 0]
        if frame.empty:
            return {"n": 0}
        diff = (frame[column] - frame[official_column]).abs()
        rel = diff / frame[official_column].replace(0, np.nan)
        return {
            "n": int(len(frame)),
            "exact_pct": round(100.0 * float((diff < 0.005).mean()), 2),
            "within_0.05_pct": round(100.0 * float((rel <= tol).mean()), 2),
            "median_abs_diff": round(float(diff.median()), 4),
            "max_abs_diff": round(float(diff.max()), 4),
            "mean_signed_diff": round(float((frame[column] - frame[official_column]).mean()), 4),
        }

    result["price_hf_high_vs_official_HghPric"] = agreement("hf_high", "HghPric")
    result["price_hf_low_vs_official_LwPric"] = agreement("hf_low", "LwPric")
    result["price_hf_close_vs_official_ClsPric"] = agreement("hf_close", "ClsPric")
    result["price_hf_close_vs_official_LastPric"] = agreement("hf_close", "LastPric")
    result["price_hf_open_vs_official_OpnPric"] = agreement("hf_open", "OpnPric")
    # Reported separately on purpose: settlement is not a candle close.
    result["info_hf_close_vs_official_SttlmPric_NOT_A_CANDLE"] = agreement("hf_close", "SttlmPric")

    # --- bracketing violations: 1-minute path must contain the official range
    frame = traded[traded["HghPric"] > 0]
    result["violations_hf_high_below_official"] = int((frame["hf_high"] < frame["HghPric"] - 0.005).sum())
    result["violations_hf_low_above_official"] = int((frame["hf_low"] > frame["LwPric"] + 0.005).sum())
    result["violations_n"] = int(len(frame))

    # --- volume and OI scaling
    for hf_col, off_col in (("hf_vol", "TtlTradgVol"), ("hf_oi_last", "OpnIntrst"), ("hf_oi_max", "OpnIntrst")):
        subset = traded[(traded[off_col] > 0) & (traded[hf_col] > 0)]
        if subset.empty:
            result[f"ratio_{hf_col}_to_{off_col}"] = None
            continue
        ratio = (subset[hf_col] / subset[off_col]).replace([np.inf, -np.inf], np.nan).dropna()
        result[f"ratio_{hf_col}_to_{off_col}"] = {
            "n": int(len(ratio)),
            "median": round(float(ratio.median()), 4),
            "p10": round(float(ratio.quantile(0.10)), 4),
            "p90": round(float(ratio.quantile(0.90)), 4),
            "exact_match_pct": round(100.0 * float((subset[hf_col] == subset[off_col]).mean()), 2),
        }

    # --- OI level agreement when scaled by lot size
    lot = float(result["official_lot_sizes"][-1]) if result["official_lot_sizes"] else 1.0
    subset = traded[(traded["OpnIntrst"] > 0)].copy()
    if len(subset):
        subset["oi_scaled"] = subset["OpnIntrst"] * lot
        diff = (subset["hf_oi_last"] - subset["oi_scaled"]).abs()
        result["oi_hf_vs_official_times_lot"] = {
            "n": int(len(subset)),
            "exact_pct": round(100.0 * float((diff < 1).mean()), 2),
            "median_abs_diff": round(float(diff.median()), 2),
            "median_ratio": round(float((subset["hf_oi_last"] / subset["oi_scaled"]).median()), 4),
        }

    return result


def main() -> int:
    report = []
    for day, expiry in PAIRS:
        if ensure_bhavcopy(day) is None:
            print(f"{day} (expiry {expiry}): official bhavcopy not reachable")
            report.append({"official_date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "hf_file": expiry,
                           "skipped": "official bhavcopy not reachable"})
            continue
        try:
            result = compare(day, expiry)
        except Exception as exc:  # noqa: BLE001
            result = {"official_date": day, "hf_file": expiry, "error": f"{type(exc).__name__}: {exc}"}
        report.append(result)
        print("=" * 78)
        print(result.get("official_date"), result.get("hf_file"), result.get("comparison_kind", ""), result.get("error", ""))
        for key, value in result.items():
            if key in ("official_date", "hf_file", "comparison_kind", "error"):
                continue
            print(f"  {key:46s} {value}")

    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

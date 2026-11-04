"""P1 step 10b - locate the open-interest unit break inside each source separately.

_p1_nse_oi_units.py showed official NSE OI magnitudes are stable across the
2024/2025 boundary while the HF/official OI ratio steps from ~1.0 to ~0.01.
That points at the HF archive, but the claim needs the archive's own numbers.

Two independent tests:

1. HF self-consistency.  For every cached options file, report the OI level the
   archive itself stores per trading day, plus HF_OI / HF_volume.  A source that
   silently rescales shows a step in its own magnitudes.
2. Official one-day identity.  OI(t) = OI(t-1) + ChngInOpnIntrst(t) only holds
   if a single source keeps one unit across t-1 and t.  Requires consecutive
   trading days, which the cached set does not provide, so two adjacent pairs
   are downloaded (one per era).

Usage:
    python scripts/_p1_oi_hf_self.py
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT = Path("logs/p1_oi_break.json")


def official_nifty(ymd: str) -> pd.DataFrame | None:
    """Cached bhavcopy, else fetch once. Returns NIFTY index-option rows."""
    path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    if not path.exists():
        url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
        try:
            from curl_cffi import requests as curl

            session = curl.Session(impersonate="chrome124")
            session.get("https://www.nseindia.com", timeout=30)
            response = session.get(url, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"  fetch failed {ymd}: {type(exc).__name__}: {exc}")
            return None
        if response.status_code != 200 or response.content[:2] != b"PK":
            print(f"  not reachable {ymd}: HTTP {response.status_code}")
            return None
        path.write_bytes(response.content)
        print(f"  downloaded {path.name}")
    frame = pd.read_csv(io.StringIO(
        zipfile.ZipFile(path).read(zipfile.ZipFile(path).namelist()[0]).decode("utf-8", "replace")))
    return frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")].copy()


def parquet_columns(path: Path) -> list[str]:
    import pyarrow.parquet as pq

    return [field.name for field in pq.read_schema(path)]


def hf_self_test() -> list[dict]:
    rows = []
    for path in sorted(HF.glob("options__NIFTY__*.parquet")):
        want = ["expiry", "strike", "option_type", "timestamp", "volume", "open_interest", "trading_day"]
        frame = pd.read_parquet(path, columns=[c for c in want if c in parquet_columns(path)])
        key = [c for c in ["symbol", "expiry", "strike", "option_type", "timestamp"] if c in frame.columns]
        frame = frame.drop_duplicates(subset=key, keep="first")
        if "trading_day" not in frame.columns:
            frame["trading_day"] = pd.to_datetime(frame["timestamp"]).dt.strftime("%Y-%m-%d")
        day = frame.groupby(["expiry", "strike", "option_type", "trading_day"], observed=True).agg(
            oi=("open_interest", "last"), vol=("volume", "sum")).reset_index()
        day = day[day["vol"] > 0]
        if day.empty:
            rows.append({"hf_file": path.name, "note": "no traded contract-days"})
            continue
        rows.append({
            "hf_file": path.name,
            "days": int(day["trading_day"].nunique()),
            "contract_days": int(len(day)),
            "median_OI": float(day["oi"].median()),
            "p90_OI": float(day["oi"].quantile(0.90)),
            "max_OI": int(day["oi"].max()),
            "median_OI_over_own_volume": round(float((day["oi"] / day["vol"]).median()), 3),
            "first_day": str(day["trading_day"].min()),
            "last_day": str(day["trading_day"].max()),
        })
    return rows


def official_identity(prev: str, cur: str) -> dict:
    a, b = official_nifty(prev), official_nifty(cur)
    if a is None or b is None:
        return {"from": prev, "to": cur, "skipped": "bhavcopy not reachable"}
    for frame in (a, b):
        frame["key"] = (frame["XpryDt"].astype(str) + "|" + frame["StrkPric"].astype(str)
                        + "|" + frame["OptnTp"].astype(str))
    merged = a[["key", "OpnIntrst"]].merge(
        b[["key", "OpnIntrst", "ChngInOpnIntrst"]], on="key", suffixes=("_prev", ""))
    if merged.empty:
        return {"from": prev, "to": cur, "skipped": "no contracts in common"}
    residual = (merged["OpnIntrst"] - merged["OpnIntrst_prev"] - merged["ChngInOpnIntrst"]).abs()
    return {
        "from": prev, "to": cur, "n": int(len(merged)),
        "identity_holds_pct": round(100.0 * float((residual <= 1).mean()), 2),
        "median_abs_residual": float(residual.median()),
        "p90_abs_residual": float(residual.quantile(0.90)),
        "median_OI_prev": float(merged["OpnIntrst_prev"].median()),
        "median_OI_cur": float(merged["OpnIntrst"].median()),
    }


def main() -> int:
    print("=== 1. HF archive, its own stored OI magnitudes per file ===")
    hf_rows = hf_self_test()
    frame = pd.DataFrame(hf_rows)
    with pd.option_context("display.width", 220, "display.max_columns", 12):
        print(frame.to_string(index=False))

    print("\n=== 2. official NSE one-day OI identity, one pair per era ===")
    pairs = [("20241218", "20241219"), ("20250122", "20250123"), ("20260223", "20260224")]
    identity = [official_identity(p, c) for p, c in pairs]
    for item in identity:
        print("  " + json.dumps(item))

    OUT.write_text(json.dumps({"hf_self": hf_rows, "official_identity": identity}, indent=2, default=str),
                   encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

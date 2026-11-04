"""Milestone-2 diagnostic 2 - classify the two residual anomalies.

A. The 2024 "+1" artifact.  Day totals matched official x lot to within 1-2 shares on
   every contract, while 2025+ matches exactly.  If the residual is a handful of bars
   whose per-minute volume is not a multiple of the lot, that is a per-bar artifact in
   the provider series, not a unit difference.  Find the offending bars and show them.

B. Why file-days were skipped.  "No officially-traded contracts" is the wrong label if
   the real cause is "no bhavcopy available for that date".  Classify every (file-day,
   expiry) pair into: OK / NO_OFFICIAL_FILE / HOLIDAY_OR_NOT_TRADING_DAY /
   EXPIRY_NOT_LISTED / LISTED_NOT_TRADED / LOT_AMBIGUOUS / NO_ARCHIVE_ROWS /
   ARCHIVE_FILE_COVERS_ONLY_FEW_DAYS, and count each.

Usage (repo root):  python scripts/_m2_diag2.py
"""

from __future__ import annotations

import io
import sys
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _p1_nse_reconcile import NSE, ensure_bhavcopy  # noqa: E402

HF = Path("data/raw/p1_sample")
KEYS = ["symbol", "expiry", "strike", "option_type", "timestamp"]


def iso(ymd: str) -> str:
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


def official_rows(ymd: str) -> pd.DataFrame | None:
    """None = no official file obtainable; empty frame = file exists, no NIFTY options."""
    path = ensure_bhavcopy(ymd)
    if path is None:
        return None
    with zipfile.ZipFile(path) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
    frame = pd.read_csv(io.StringIO(text))
    nifty = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")].copy()
    if nifty.empty:
        return pd.DataFrame()
    nifty["strike"] = nifty["StrkPric"].astype(float).round(0).astype("int64")
    nifty["option_type"] = nifty["OptnTp"]
    nifty["expiry"] = pd.to_datetime(nifty["XpryDt"], errors="coerce").dt.strftime("%Y-%m-%d")
    return nifty


def artifact(ymd: str, expiry: str) -> None:
    print(f"\n=== A. per-bar artifact check {iso(ymd)} x {expiry} ===")
    off = official_rows(ymd)
    if off is None or off.empty:
        print("  no official arbiter")
        return
    lot = sorted(set(pd.to_numeric(off["NewBrdLotQty"], errors="coerce").dropna().astype("int64")))
    if len(lot) != 1:
        print(f"  lot ambiguous: {lot}")
        return
    lot = lot[0]
    path = HF / f"options__NIFTY__{expiry}.parquet"
    if not path.exists():
        print(f"  archive file not cached: {path.name}")
        return
    day = pd.read_parquet(path).drop_duplicates(subset=KEYS, keep="first")
    day = day[(day["trading_day"] == iso(ymd))].copy()
    day["ts"] = pd.to_datetime(day["timestamp"])
    day["clock"] = day["ts"].dt.strftime("%H:%M")
    day["mod"] = day["volume"].astype("int64") % lot
    bad = day[day["mod"] != 0]
    print(f"  lot {lot}: bars total {len(day)}, bars not divisible by lot {len(bad)}, "
          f"affected contracts {bad.groupby(['strike','option_type'], observed=True).ngroups}")
    if not bad.empty:
        print(f"  distinct offending volumes: {sorted(bad['volume'].unique())[:12]}")
        print(f"  offending clock labels: {dict(Counter(bad['clock']).most_common(8))}")
        print(bad[["clock", "strike", "option_type", "volume", "open", "high", "low", "close"]]
              .sort_values("clock").head(10).to_string(index=False))
    # session clock range over the whole day
    print(f"  clock range {day['clock'].min()} .. {day['clock'].max()}, "
          f"distinct minutes {day['clock'].nunique()}")


def classify() -> None:
    print("\n=== B. reason every (file-day, expiry) pair was or was not usable ===")
    expiries = sorted(p.stem.replace("options__NIFTY__", "") for p in HF.glob("options__NIFTY__*.parquet"))
    reasons: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for expiry in expiries:
        path = HF / f"options__NIFTY__{expiry}.parquet"
        days = sorted(pd.read_parquet(path, columns=["trading_day"])["trading_day"].astype(str).unique())
        for day in days:
            ymd = day.replace("-", "")
            off = official_rows(ymd)
            if off is None:
                # The free NSE F&O bhavcopy archive starts 2024-01-02 (measured in P1), so
                # a 404 before that date is "outside the official archive", not "holiday".
                key = "BEFORE_OFFICIAL_ARCHIVE_FLOOR" if ymd < "20240102" else "NO_OFFICIAL_FILE"
            elif off.empty:
                key = "NO_NIFTY_OPTIONS_IN_FILE"
            else:
                sub = off[off["expiry"] == expiry]
                if sub.empty:
                    key = "EXPIRY_NOT_LISTED"
                else:
                    lots = set(pd.to_numeric(sub["NewBrdLotQty"], errors="coerce").dropna().astype("int64"))
                    traded = sub[pd.to_numeric(sub["TtlTradgVol"], errors="coerce").fillna(0) > 0]
                    hf_day = pd.read_parquet(path, columns=["trading_day"])
                    n_hf = int((hf_day["trading_day"].astype(str) == day).sum())
                    if len(lots) != 1:
                        key = "LOT_AMBIGUOUS"
                    elif traded.empty:
                        key = "LISTED_NOT_TRADED"
                    elif n_hf == 0:
                        key = "NO_ARCHIVE_ROWS"
                    else:
                        key = "OK"
            reasons[key] += 1
            examples.setdefault(key, []).append(f"{day}x{expiry}")
    for key, count in reasons.most_common():
        sample = examples[key][:6]
        print(f"  {key:26s} {count:5d}  e.g. {', '.join(sample)}")
    print(f"  total pairs examined: {sum(reasons.values())}")


def main() -> int:
    pd.set_option("display.width", 220)
    artifact("20240118", "2024-01-25")
    artifact("20240718", "2024-07-25")
    artifact("20250320", "2025-03-27")
    artifact("20260217", "2026-02-24")
    classify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

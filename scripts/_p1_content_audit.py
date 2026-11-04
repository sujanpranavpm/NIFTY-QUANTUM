"""P1 step 4 - content-level integrity audit of the sampled options files.

Works only on the small sample already cached in ``data/raw/p1_sample``.
Every check is a real measurement on real rows; nothing is inferred from the
dataset card.

Usage:
    python scripts/_p1_content_audit.py [cached_file_stem ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path("data/raw/p1_sample")
EXPECTED_MINUTES = 376  # 09:15 .. 15:30 inclusive


def key_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in ("symbol", "expiry", "strike", "option_type") if c in df.columns]


def audit(path: Path) -> dict:
    df = pd.read_parquet(path)
    out: dict = {"file": path.name, "rows": int(len(df)), "columns": list(df.columns)}
    if df.empty:
        out["EMPTY"] = True
        return out

    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        out["tz_naive"] = True
    else:
        out["tz"] = str(ts.dt.tz)
    out["timestamp_min"] = str(ts.min())
    out["timestamp_max"] = str(ts.max())
    out["trading_days"] = int(df["trading_day"].nunique()) if "trading_day" in df else None
    out["trading_day_range"] = [str(df["trading_day"].min()), str(df["trading_day"].max())] if "trading_day" in df else None
    if "expiry" in df:
        out["distinct_expiries"] = sorted(df["expiry"].astype(str).unique())[:8]
        out["n_expiries"] = int(df["expiry"].nunique())
    if "option_type" in df:
        out["option_type_counts"] = df["option_type"].value_counts().to_dict()
    if "strike" in df:
        strikes = sorted(int(s) for s in df["strike"].unique())
        out["n_strikes"] = len(strikes)
        out["strike_min"] = strikes[0]
        out["strike_max"] = strikes[-1]
        diffs = np.diff(strikes)
        out["strike_spacing_counts"] = {int(k): int(v) for k, v in zip(*np.unique(diffs, return_counts=True))}
    out["contracts"] = int(df.groupby(key_cols(df), observed=True).ngroups)

    # ---- timestamp grain and duplicates
    minute_mod = ts.dt.floor("min")
    out["is_whole_minute"] = bool((ts == minute_mod).all())
    dup_mask = df.duplicated(subset=key_cols(df) + ["timestamp"], keep=False)
    out["duplicate_contract_timestamps"] = int(dup_mask.sum())

    # ---- OHLC validity
    for col in ("open", "high", "low", "close"):
        out[f"null_{col}"] = int(df[col].isna().sum())
        out[f"nonpositive_{col}"] = int((df[col] <= 0).sum())
    bad_hl = int(((df["high"] < df["low"]) | (df["high"] < df["open"]) | (df["high"] < df["close"])
                  | (df["low"] > df["open"]) | (df["low"] > df["close"])).sum())
    out["ohlc_order_violations"] = bad_hl
    out["zero_range_bars"] = int((df["high"] == df["low"]).sum())

    # ---- volume / OI population
    if "volume" in df:
        out["volume_zero_rows"] = int((df["volume"] == 0).sum())
        out["volume_nulls"] = int(df["volume"].isna().sum())
        out["volume_min"] = float(df["volume"].min())
        out["volume_max"] = float(df["volume"].max())
    if "open_interest" in df:
        oi = df["open_interest"]
        out["oi_nulls"] = int(oi.isna().sum())
        out["oi_zero_rows"] = int((oi == 0).sum())
        out["oi_min"] = float(oi.min())
        out["oi_max"] = float(oi.max())
        out["oi_equals_volume_rows"] = int((oi == df["volume"]).sum())

    # ---- session coverage per contract-day
    if "trading_day" in df:
        grouped = df.groupby(["trading_day"], observed=True)["timestamp"]
        span = grouped.agg(["min", "max", "count", "nunique"])
        out["session_first_time_counts"] = {
            str(k).split()[-1]: int(v) for k, v in span["min"].dt.strftime("%H:%M").value_counts().items()
        }
        out["session_last_time_counts"] = {
            str(k).split()[-1]: int(v) for k, v in span["max"].dt.strftime("%H:%M").value_counts().items()
        }
        out["distinct_minutes_per_day_min"] = int(span["nunique"].min())
        out["distinct_minutes_per_day_median"] = float(span["nunique"].median())
        out["distinct_minutes_per_day_max"] = int(span["nunique"].max())

    # ---- OI behaviour test: the decisive check on whether OI is genuine
    if "open_interest" in df and "trading_day" in df:
        sample = df[df["open_interest"] > df["open_interest"].quantile(0.5)]
        grp_cols = key_cols(df) + ["trading_day"]
        # Restrict to contract-days with a full session so the test is fair.
        counts = sample.groupby(grp_cols, observed=True).size()
        keep = counts[counts >= 300].index[:200]
        if len(keep):
            sub = sample.set_index(grp_cols)
            sub = sub.loc[[tuple(k) for k in keep]].reset_index()
            sub = sub.sort_values(grp_cols + ["timestamp"])
            g = sub.groupby(grp_cols, observed=True, sort=False)
            delta = g["open_interest"].diff()
            n = g["volume"].transform("size")
            changes = int((delta.fillna(0) != 0).sum())
            decreases = int((delta < 0).sum())
            out["oi_test_contracts_days"] = int(len(keep))
            out["oi_test_rows"] = int(len(sub))
            out["oi_changes_pct"] = round(100.0 * changes / max(1, len(sub)), 2)
            out["oi_decreases"] = decreases
            out["oi_strictly_monotonic_nonzero_share"] = round(
                float(g["open_interest"].apply(lambda s: bool(s.is_monotonic_increasing)).mean()), 4
            )
            # Is OI just cumulative volume? Compare OI to running sum of volume.
            run = sub.groupby(grp_cols, observed=True, sort=False)["volume"].cumsum()
            ratio = (sub["open_interest"] / run.replace(0, np.nan)).dropna()
            out["oi_vs_cumvolume_ratio_median"] = round(float(ratio.median()), 4) if len(ratio) else None
            out["oi_equals_cumvolume_rows"] = int((sub["open_interest"] == run).sum())
        else:
            out["oi_test"] = "no contract-day with >=300 rows in sample"

    # ---- intraday price discontinuity sniff
    if "close" in df:
        sub = df.sort_values(key_cols(df) + ["timestamp"])
        ret = sub.groupby(key_cols(df), observed=True)["close"].pct_change()
        out["abs_ret_p99"] = round(float(ret.abs().quantile(0.99)), 5)
        out["abs_ret_max"] = round(float(ret.abs().max()), 5)
        out["bars_over_5pct_move"] = int((ret.abs() > 0.05).sum())

    return out


def main() -> int:
    args = sys.argv[1:]
    if args:
        files = [CACHE / (a if a.endswith(".parquet") else f"{a}.parquet") for a in args]
    else:
        files = sorted(CACHE.glob("*.parquet"))
    report = []
    for path in files:
        if not path.exists():
            print(f"MISSING {path}")
            continue
        result = audit(path)
        report.append(result)
        print("=" * 78)
        print(path.name)
        print("=" * 78)
        for key, value in result.items():
            if key == "columns":
                continue
            print(f"  {key:38s} {value}")

    Path("logs/p1_content_audit.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("\nwrote logs/p1_content_audit.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

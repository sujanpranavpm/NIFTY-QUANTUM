"""P1 step 5 - forensics on the two worst defects found by the content audit.

1. Contract-minute duplicate rows (catastrophic in some files).
2. Bars outside the 09:15-15:30 NSE cash session.
3. Trading days that are drastically truncated.

Prints concrete example rows so the defect can be judged, not just counted.

Usage:
    python scripts/_p1_forensics.py [file_stem ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

CACHE = Path("data/raw/p1_sample")
KEYS = ["symbol", "expiry", "strike", "option_type", "timestamp"]

DEFAULT = [
    "options__NIFTY__2021-12-30",
    "options__NIFTY__2022-09-29",
    "options__NIFTY__2025-01-30",
    "options__NIFTY__2024-01-25",
    "options__NIFTY__2021-05-27",
]


def keys_for(df: pd.DataFrame) -> list[str]:
    return [k for k in KEYS if k in df.columns]


def report(path: Path) -> None:
    df = pd.read_parquet(path)
    keys = keys_for(df)
    print("=" * 78)
    print(path.name, f"rows={len(df):,}  cols={list(df.columns)}")
    print("=" * 78)

    dup = df.duplicated(subset=keys, keep=False)
    print(f"duplicate contract-minute rows: {int(dup.sum()):,} ({100.0 * dup.mean():.1f}%)")
    if dup.any():
        example = df[dup].sort_values(keys).head(8)
        print("  example duplicate block:")
        print(example.to_string(index=False))
        # Are duplicates identical in every column, or do they disagree?
        full_dup = int(df.duplicated(keep=False).sum())
        print(f"  rows duplicated across ALL columns: {full_dup:,}")
        grouped = df[dup].groupby(keys, observed=True).agg(
            n=("close", "size"), distinct_close=("close", "nunique"), distinct_oi=("open_interest", "nunique")
        )
        print(f"  distinct contract-minutes affected : {len(grouped):,}")
        print(f"  affected minutes where prices differ: {int((grouped['distinct_close'] > 1).sum()):,}")
        print(f"  affected minutes where OI differs   : {int((grouped['distinct_oi'] > 1).sum()):,}")
        print(f"  mean rows per affected minute      : {grouped['n'].mean():.2f}")
        # Does dropping duplicates restore the expected day structure?
        dedup = df.drop_duplicates(subset=keys, keep="first")
        print(f"  rows after dedup: {len(dedup):,}   trading_days after dedup: {dedup['trading_day'].nunique()}")

    if "trading_day" in df:
        contract_col = "strike" if "strike" in df.columns else "symbol"
        per_day = df.groupby("trading_day", observed=True).agg(
            rows=("timestamp", "size"),
            minutes=("timestamp", "nunique"),
            first=("timestamp", "min"),
            last=("timestamp", "max"),
            contracts=(contract_col, "nunique"),
        )
        print("\nper trading-day shape:")
        print(per_day.to_string())

    hhmm = pd.to_datetime(df["timestamp"]).dt.strftime("%H:%M")
    outside = df[(hhmm < "09:15") | (hhmm > "15:30")]
    print(f"\nbars outside 09:15-15:30: {len(outside):,} ({100.0 * len(outside) / max(1, len(df)):.2f}%)")
    if len(outside):
        print("  time-of-day distribution outside session:")
        print("   ", outside["timestamp"].dt.strftime("%H:%M").value_counts().head(12).to_dict())
        print("  example rows:")
        print(outside.head(6).to_string(index=False))


def main() -> int:
    stems = sys.argv[1:] or DEFAULT
    for stem in stems:
        path = CACHE / f"{stem}.parquet"
        if not path.exists():
            print(f"MISSING {path}")
            continue
        report(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

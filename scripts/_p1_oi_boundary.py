"""P1 step 18 - pin the open-interest scale break to a calendar day.

`_p1_oi_repair.py` proved the archive stores OI in contracts up to the 2024-12-26
era and at 1/100 of that from the 2025-01-20 era on, but the cached sample has no
expiry file whose rows fall between 2024-12-26 and 2025-01-20, so the break was
only bounded to a three-week window.  The ingestion layer needs a date, not a
window, so the archive's 2025-01-16 expiry file is fetched: its rows cover
December 2024 straight through 16 January 2025 and therefore cross the boundary.

For each trading day in that range the ratio official_OI / archive_OI is measured
on contracts the exchange reports as traded with nonzero open interest.  A ratio
near 1 means the archive is still in contracts; a ratio near 100 means it has
already moved to the 1/100 convention.

Usage:
    python scripts/_p1_oi_boundary.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path("scripts").resolve()))
from _p1_hf_schema import fetch                                  # noqa: E402
from _p1_nse_reconcile import ensure_bhavcopy, load_official    # noqa: E402

OUT = Path("logs/p1_oi_boundary.json")
# weekly expiries, oldest first: each file holds roughly its own two-week life,
# so the three together cover mid-December through mid-January.
EXPIRIES = ["2025-01-02", "2025-01-09", "2025-01-16"]
START, STOP = date(2024, 12, 16), date(2025, 1, 16)


def load_expiry(expiry: str) -> pd.DataFrame:
    path = fetch(f"options/NIFTY/{expiry}.parquet")
    frame = pd.read_parquet(path)
    frame = frame.drop_duplicates(
        subset=["symbol", "expiry", "strike", "option_type", "timestamp"], keep="first")
    return frame[frame["expiry"] == expiry]


def main() -> int:
    frames = {e: load_expiry(e) for e in EXPIRIES}
    for expiry, frame in frames.items():
        days = sorted(frame["trading_day"].unique())
        print(f"options/NIFTY/{expiry}.parquet rows={len(frame):,} "
              f"days {days[0]}..{days[-1]} (n={len(days)})")

    rows = []
    day = START
    while day <= STOP:
        ymd = day.strftime("%Y%m%d")
        if not ensure_bhavcopy(ymd):
            day += timedelta(days=1)
            continue
        iso = day.strftime("%Y-%m-%d")
        # the contract's own expiry is the nearest one that actually has bars for
        # this day, which is what the exchange report must be filtered to
        expiry = next((e for e in EXPIRIES if e >= iso and not frames[e][
            frames[e]["trading_day"] == iso].empty), None)
        if expiry is None:
            day += timedelta(days=1)
            continue
        official = load_official(ymd)
        official = official[(official["expiry"] == expiry) & (official["TtlTradgVol"] > 0)
                            & (official["OpnIntrst"].astype(float) > 0)]
        hf_day = frames[expiry][frames[expiry]["trading_day"] == iso]
        if official.empty or hf_day.empty:
            day += timedelta(days=1)
            continue
        last = hf_day.groupby(["strike", "option_type"], observed=True, sort=False)[
            "open_interest"].last().reset_index()
        merged = official.merge(last, on=["strike", "option_type"], how="inner")
        used = merged[merged["open_interest"] > 0]
        ratio = (used["OpnIntrst"].astype(float) / used["open_interest"]) if len(used) else pd.Series(dtype=float)
        rows.append({
            "date": iso,
            "hf_expiry_used": expiry,
            "official_traded_oi_contracts": int(len(merged)),
            "archive_nonzero_pct": round(100 * len(used) / max(len(merged), 1), 1),
            "ratio_median": round(float(ratio.median()), 3) if len(ratio) else None,
            "ratio_p10": round(float(ratio.quantile(0.10)), 3) if len(ratio) else None,
            "ratio_p90": round(float(ratio.quantile(0.90)), 3) if len(ratio) else None,
            "convention": ("contracts" if len(ratio) and ratio.median() < 10 else
                           "1/100 of contracts" if len(ratio) else "no data"),
        })
        day += timedelta(days=1)

    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    flips = [i for i in range(1, len(table))
             if table.at[i - 1, "convention"] != table.at[i, "convention"]
             and table.at[i, "convention"] != "no data"]
    print("\nconvention changes observed on:")
    for i in flips:
        print(f"  {table.at[i - 1, 'date']} [{table.at[i - 1, 'convention']}]  ->  "
              f"{table.at[i, 'date']} [{table.at[i, 'convention']}]")
    OUT.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P1 step 14b - which price grid does the exchange actually use for NIFTY options?

NSE's contract-specifications page publishes a tick table keyed on *index level*
(0-15000 -> 0.05, >15000-30000 -> 0.10, >30000 -> 0.20, circular NSE/FAOP/67135).
NIFTY crossed 15,000 in late 2023 and 20,000 in 2024, so a literal reading says
NIFTY option premiums should have moved to a 0.10 tick - yet the 1-minute archive
keeps a large minority of prices on odd 0.05 steps.

Whichever way this resolves it matters for backtests, because tick size sets the
spread floor.  The exchange's own reported prices settle it: if the bhavcopy uses
the same grid as the archive, the archive is faithful; if not, the archive is
finer or coarser than the real market.

Usage:
    python scripts/_p1_tick_grid.py
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT = Path("logs/p1_tick_grid.json")
PRICE_COLS = ["OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric", "PrvsClsgPric", "SttlmPric"]


def cents(series: pd.Series) -> pd.Series:
    return (series.astype(float) * 100).round(0).astype("int64")


def grid_stats(values: pd.Series) -> dict:
    values = values[(values.notna()) & (values > 0.005)]
    if values.empty:
        return {"n": 0}
    c = cents(values)
    return {
        "n": int(len(c)),
        "pct_0.05": round(100.0 * float((c % 5 == 0).mean()), 2),
        "pct_0.10": round(100.0 * float((c % 10 == 0).mean()), 2),
        "pct_0.05_only": round(100.0 * float(((c % 5 == 0) & (c % 10 != 0)).mean()), 2),
        "off_grid_pct": round(100.0 * float((c % 5 != 0).mean()), 2),
    }


def main() -> int:
    rows = []
    cached = sorted({p.stem.split("_")[-1] for p in NSE.glob("BhavCopy_NSE_FO_*.zip")})
    for ymd in (c for c in cached if c.isdigit() and len(c) == 8):
        path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
        if not path.exists():
            continue
        raw = zipfile.ZipFile(path)
        frame = pd.read_csv(io.StringIO(raw.read(raw.namelist()[0]).decode("utf-8", "replace")))
        opts = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")]
        if opts.empty:
            continue
        index_level = pd.to_numeric(opts["UndrlygPric"], errors="coerce").max()
        prices = pd.concat([pd.to_numeric(opts[c], errors="coerce") for c in PRICE_COLS])
        rows.append({"source": "official", "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                     "nifty_index_close": round(float(index_level), 2), **grid_stats(prices)})

    for path in sorted(HF.glob("options__NIFTY__*.parquet")):
        frame = pd.read_parquet(path, columns=["open", "high", "low", "close", "trading_day"])
        iso = str(pd.to_datetime(frame["trading_day"]).max())[:10]
        prices = pd.concat([frame[c] for c in frame.columns if c != "trading_day"])
        rows.append({"source": "hf_archive", "date": iso, "nifty_index_close": None,
                     **grid_stats(prices)})

    frame = pd.DataFrame(rows).sort_values(["date", "source"])
    with pd.option_context("display.width", 220, "display.max_columns", 12):
        print(frame[frame["n"] > 0].to_string(index=False))
    print("\nexpected tick per the index-level slab: 0-15000 -> 0.05, "
          ">15000-30000 -> 0.10, >30000 -> 0.20")
    OUT.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

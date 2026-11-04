"""P1 step 14c - isolate the 2026 price-step change.

The grid test showed every official NIFTY option price sitting on the 0.05 grid
until 2026-07-28, then 6-9% off it from 2026-08-03.  Two very different
explanations fit that: the exchange genuinely refined the tick, or a derived
column (settlement / previous close) simply carries more decimals while traded
prices stay on 0.05.  The difference matters for backtest spread assumptions, so
the columns are separated and the boundary date is narrowed.

Usage:
    python scripts/_p1_tick_change.py
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd
from curl_cffi import requests as curl

NSE = Path("data/raw/p1_nse")
TRADED = ["OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric"]
DERIVED = ["PrvsClsgPric", "SttlmPric"]


def rows_for(ymd: str) -> pd.DataFrame | None:
    path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    if not path.exists():
        url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
        response = SESSION.get(url, timeout=60)
        if response.status_code != 200 or response.content[:2] != b"PK":
            return None
        path.write_bytes(response.content)
    raw = zipfile.ZipFile(path)
    frame = pd.read_csv(io.StringIO(raw.read(raw.namelist()[0]).decode("utf-8", "replace")))
    out = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")]
    return out if not out.empty else None


def off_grid(values: pd.Series, step_paise: int = 5) -> tuple[int, float]:
    values = pd.to_numeric(values, errors="coerce")
    values = values[(values.notna()) & (values > 0.005)]
    if values.empty:
        return 0, 0.0
    cents = (values * 100).round(0).astype("int64")
    return int(len(cents)), round(100.0 * float((cents % step_paise != 0).mean()), 2)


def main() -> int:
    global SESSION
    SESSION = curl.Session(impersonate="chrome124")
    SESSION.get("https://www.nseindia.com", timeout=30)

    # every trading day between the last clean date and the first dirty date
    candidates = ["20260728", "20260729", "20260730", "20260731", "20260801",
                  "20260803", "20260804", "20260805", "20260806", "20260810"]
    out = []
    for ymd in candidates:
        frame = rows_for(ymd)
        if frame is None:
            print(f"  {ymd}: no report (holiday or not yet published)")
            continue
        traded_n, traded_off = off_grid(pd.concat([frame[c] for c in TRADED]))
        derived_n, derived_off = off_grid(pd.concat([frame[c] for c in DERIVED]))
        last_n, last_off = off_grid(frame["LastPric"])
        print(f"  {ymd}: traded-price off-0.05 = {traded_off:5.2f}% (n={traded_n:,}) | "
              f"derived off = {derived_off:5.2f}% | LastPric off = {last_off:5.2f}%")
        out.append({"date": ymd, "traded_n": traded_n, "traded_off_pct": traded_off,
                    "derived_n": derived_n, "derived_off_pct": derived_off,
                    "lastpric_off_pct": last_off})

    Path("logs/p1_tick_change.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P1 step 12 - let the exchange state which expiries existed.

The expiry-calendar test left four open questions that a rule of thumb cannot
answer:

  * BANKNIFTY 2024-03-27 (Wed) and 2024-12-24 (Tue) sit on a weekday the regime
    table does not allow, and the nominal last-Thursday was a trading day.
  * NIFTY has no file for 2025-10-21 and none for 2026-06-16/23/30, although
    those were trading days.

The official bhavcopy answers this directly: on any trading day it lists every
contract then available, and XpryDt gives its expiry.  If a date appears as an
XpryDt in the exchange report but has no Parquet file, the archive is missing it.
If it does not appear, the archive is correct and the regime table was wrong.

Usage:
    python scripts/_p1_nse_listed_expiries.py
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
from curl_cffi import requests as curl

NSE = Path("data/raw/p1_nse")
COVERAGE = Path("logs/p1_hf_coverage.json")
OUT = Path("logs/p1_nse_listed_expiries.json")

# probe-day -> the expiry dates whose existence is in question
PROBES = {
    "20240327": ["2024-03-27", "2024-03-28"],           # BANKNIFTY Wednesday start
    "20241224": ["2024-12-24", "2024-12-26"],           # BANKNIFTY Tuesday case
    "20251014": ["2025-10-21", "2025-10-28"],           # NIFTY missing weekly
    "20260609": ["2026-06-16", "2026-06-23", "2026-06-30", "2026-07-07"],
    "20260723": ["2026-07-30", "2026-08-04", "2026-08-11"],
}


def load(ymd: str) -> pd.DataFrame | None:
    path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    if not path.exists():
        url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
        session = curl.Session(impersonate="chrome124")
        session.get("https://www.nseindia.com", timeout=30)
        response = session.get(url, timeout=60)
        if response.status_code != 200 or response.content[:2] != b"PK":
            print(f"  {ymd}: not reachable (HTTP {response.status_code})")
            return None
        path.write_bytes(response.content)
        print(f"  {ymd}: downloaded {len(response.content):,}B")
        time.sleep(0.4)
    raw = zipfile.ZipFile(path)
    frame = pd.read_csv(io.StringIO(
        raw.read(raw.namelist()[0]).decode("utf-8", "replace")))
    return frame[frame["FinInstrmTp"].isin(["IDO", "STO", "IDF", "STF"])].copy()


def main() -> int:
    coverage = json.loads(COVERAGE.read_text(encoding="utf-8"))
    hf_expiries = {
        symbol: {e["date"] for e in block["files"] if e.get("date")}
        for symbol, block in coverage.items()
    }
    report = []
    for ymd, questions in PROBES.items():
        frame = load(ymd)
        if frame is None:
            report.append({"probe_day": ymd, "skipped": "bhavcopy not reachable"})
            continue
        listed = sorted(pd.to_datetime(frame["XpryDt"]).dt.strftime("%Y-%m-%d").unique())
        near = [d for d in listed if d[:4] == questions[0][:4]]
        print("=" * 96)
        print(f"report of {ymd}  |  expiries listed nearby: {' '.join(near)}")
        for q in questions:
            rows = frame[pd.to_datetime(frame["XpryDt"]).dt.strftime("%Y-%m-%d") == q]
            by_symbol = sorted(rows["TckrSymb"].unique())
            print("-" * 96)
            print(f"  expiry {q}: listed on exchange for {by_symbol or 'no symbol'}")
            for symbol_key, prefix in (("options/NIFTY", "NIFTY"), ("options/BANKNIFTY", "BANKNIFTY")):
                present = q in hf_expiries.get(symbol_key, set())
                on_exchange = prefix in by_symbol
                verdict = ("match" if present == on_exchange else
                           "ARCHIVE MISSING (exchange listed it, no Parquet file)" if on_exchange
                           else "ARCHIVE EXTRA (file exists, exchange did not list it)")
                print(f"      {symbol_key:22s} hf_file={present!s:6s} exchange_listed={on_exchange!s:6s} {verdict}")
            report.append({
                "probe_day": ymd, "expiry_questioned": q,
                "symbols_listed": by_symbol,
                "n_contracts": int(len(rows)),
                "all_expiries_in_report": listed,
            })

    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

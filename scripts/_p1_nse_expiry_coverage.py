"""P1 step 13 - official expiry coverage: every month, measured, not assumed.

A hand-written expiry-regime table proved unreliable: the exchange's own reports
showed BANKNIFTY expiring on 2024-03-27 (Wednesday) and 2024-12-24 (Tuesday),
dates the table rejected.  So the table is dropped as an arbiter and the reports
become the arbiter.

Method.  One bhavcopy per calendar month, taken from an early trading day of that
month, lists every index-option expiry the exchange had for that month.  The
Hugging Face archive stores one Parquet file per expiry, so the two sets can be
compared directly.  This yields a real missing-expiry report for the window the
free official archive covers (2024-01-02 onwards) and costs about one download
per month.

SENSEX is deliberately excluded: it is a BSE product and does not appear in NSE
reports, so its coverage cannot be audited this way.

Usage:
    python scripts/_p1_nse_expiry_coverage.py
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from calendar import monthrange
from pathlib import Path

import pandas as pd
from curl_cffi import requests as curl

NSE = Path("data/raw/p1_nse")
COVERAGE = Path("logs/p1_hf_coverage.json")
OUT = Path("logs/p1_nse_expiry_coverage.json")

MONTHS = [f"{y}{m:02d}" for y in (2024, 2025, 2026) for m in range(1, 13)]
SYMBOLS = {"NIFTY": "options/NIFTY", "BANKNIFTY": "options/BANKNIFTY"}


def fetch(ymd: str) -> pd.DataFrame | None:
    """Return the derivatives rows of that day's bhavcopy, or None if the day
    was not a trading day (or is unreachable)."""
    path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    if not path.exists():
        url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
        response = SESSION.get(url, timeout=60)
        if response.status_code != 200 or response.content[:2] != b"PK":
            return None
        path.write_bytes(response.content)
        time.sleep(0.3)
    raw = zipfile.ZipFile(path)
    frame = pd.read_csv(io.StringIO(raw.read(raw.namelist()[0]).decode("utf-8", "replace")))
    return frame[frame["FinInstrmTp"] == "IDO"].copy()


def report_for_month(ym: str) -> dict:
    """Try days 3..11 of the month until a trading day is found."""
    year, month = int(ym[:4]), int(ym[4:])
    for day in range(3, 12):
        if day > monthrange(year, month)[1]:
            break
        ymd = f"{ym}{day:02d}"
        rows = fetch(ymd)
        if rows is None:
            continue
        out = {"probe_day": ymd, "symbols": {}}
        for ticker, hf_key in SYMBOLS.items():
            listed = sorted(set(pd.to_datetime(
                rows[rows["TckrSymb"] == ticker]["XpryDt"]).dt.strftime("%Y-%m-%d")))
            listed = [d for d in listed if d.startswith(f"{ym[:4]}-{ym[4:]}")]
            out["symbols"][ticker] = {"hf_key": hf_key, "exchange_expiries": listed}
        return out
    return {"probe_day": None, "error": f"no trading day found in {ym} days 3-11"}


def main() -> int:
    NSE.mkdir(parents=True, exist_ok=True)
    SESSION.get("https://www.nseindia.com", timeout=30)
    time.sleep(1.0)

    coverage = json.loads(COVERAGE.read_text(encoding="utf-8"))
    hf = {key: {e["date"] for e in block["files"] if e.get("date")} for key, block in coverage.items()}
    hf_last = {key: max(dates) for key, dates in hf.items() if dates}

    rows, totals = [], {t: {"listed": 0, "missing": 0, "beyond_span": 0} for t in SYMBOLS}
    for ym in MONTHS:
        info = report_for_month(ym)
        iso = f"{ym[:4]}-{ym[4:]}-01"
        if info.get("probe_day") is None:
            print(f"{iso}  {info['error']}")
            rows.append({"month": iso, **info})
            continue
        line = [f"{iso}  probe {info['probe_day']}"]
        for ticker, data in info["symbols"].items():
            listed = set(data["exchange_expiries"])
            present = {d for d in listed if d in hf[data["hf_key"]]}
            missing = sorted(listed - present)
            # Anything after the archive's own last file is simply not covered
            # yet, which is a span limit rather than a hole inside the archive.
            inside = [d for d in missing if d <= hf_last[data["hf_key"]]]
            beyond = [d for d in missing if d > hf_last[data["hf_key"]]]
            totals[ticker]["listed"] += len(listed)
            totals[ticker]["missing"] += len(inside)
            totals[ticker]["beyond_span"] += len(beyond)
            data["archive_has"] = sorted(present)
            data["archive_missing"] = inside
            data["beyond_archive_span"] = beyond
            line.append(f"{ticker}: {len(listed)} expiries, holes {inside or 'none'}, "
                        f"beyond span {len(beyond)}")
        print("  " + "  |  ".join(line))
        rows.append({"month": iso, **info})

    print("\n" + "=" * 90)
    for ticker, t in totals.items():
        pct = 100.0 * t["missing"] / max(1, t["listed"])
        print(f"{ticker:10s} official expiries audited: {t['listed']:4d}   "
              f"missing INSIDE the archive span: {t['missing']:3d} ({pct:.1f}%)   "
              f"after archive end: {t['beyond_span']:3d}")
    print("\nonly 2024-01-02 onwards is auditable; the free official archive does not")
    print("serve bhavcopies for earlier dates, so 2021-2023 expiries stay UNVERIFIED.")

    OUT.write_text(json.dumps({"per_month": rows, "totals": totals}, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    SESSION = curl.Session(impersonate="chrome124")
    raise SystemExit(main())

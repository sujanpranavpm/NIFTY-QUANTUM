"""P1 step 7e - discover a working official NSE end-of-day derivatives report.

Two candidate mechanisms are tested with real requests:

1. the "All Reports" archive downloader  /api/ReportsDataFile?reportname=...
2. direct nsearchives BhavCopy_NSE_* file names (current and legacy layouts)

The goal is a free, public, per-contract daily report carrying traded volume,
open interest, day high/low and settlement price, which is the only official
object comparable to the Hugging Face archive.
"""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.parse
import zipfile
from pathlib import Path

from curl_cffi import requests as curl

RAW = Path("data/raw/p1_nse")
RAW.mkdir(parents=True, exist_ok=True)
OUT = Path("logs/p1_nse_reports.json")

DAY_ISO = "20260825"
DAY_DMY = "25-08-2026"
DAY_OLD = "25082026"

REPORT_NAMES = [
    "bhavcopy_dei",
    "bhavcopy_fo",
    "bhavcopy_derrivatives",
    "securitywisederivatives",
    "fno_bhavcopy",
]

SUBCATEGORIES = ["index", "security", "all"]

DIRECT_URLS = [
    f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{DAY_ISO}_F_0000.csv.zip",
    f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_DERIVATIVES_CM_0_0_0_{DAY_ISO}_F_0000.csv.zip",
    f"https://nsearchives.nseindia.com/content/derivatives/BhavCopy_NSE_DERIVATIVES_0_0_0_{DAY_ISO}_F_0000.csv.zip",
    f"https://nsearchives.nseindia.com/content/fo/fo_bhavcopy_{DAY_OLD}.csv",
    f"https://nsearchives.nseindia.com/content/fo/FO_ScriptWiseDerivatives{DAY_ISO}.csv.zip",
    f"https://nsearchives.nseindia.com/content/fo/FO_SecurityWiseQuote_{DAY_OLD}.csv",
    f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2026/AUG/fo{DAY_OLD}.csv.zip",
    f"https://nsearchives.nseindia.com/new/reportsdata/v3/fo/dei/bhavcopy/2026/202608/BhavCopy_{DAY_ISO}.csv.zip",
]


def try_reports_data_file(session: curl.Session) -> list[dict]:
    results = []
    for report in REPORT_NAMES:
        for sub in SUBCATEGORIES:
            query = {
                "reportname": report,
                "compressed": "true",
                "brmsdate": DAY_DMY,
                "type": "daily",
                "category": "EQ",
                "subcategory": sub,
            }
            url = "https://www.nseindia.com/api/ReportsDataFile?" + urllib.parse.urlencode(query)
            try:
                response = session.get(url, timeout=45)
            except Exception as exc:  # noqa: BLE001
                results.append({"report": report, "sub": sub, "error": f"{type(exc).__name__}: {exc}"})
                continue
            record = {"report": report, "sub": sub, "url": url, "status": response.status_code}
            body = response.content
            record["bytes"] = len(body)
            if response.status_code == 200 and body[:2] == b"PK":
                name = f"ReportsDataFile_{report}_{sub}.zip"
                (RAW / name).write_bytes(body)
                record["zip_saved"] = name
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    record["members"] = archive.namelist()
                    head = archive.read(archive.namelist()[0]).decode("utf-8", "replace").splitlines()
                    record["csv_header"] = head[0] if head else ""
                    record["csv_rows"] = len(head)
                    record["csv_sample"] = head[1] if len(head) > 1 else ""
            elif response.status_code == 200:
                record["body_head"] = body[:120].decode("utf-8", "replace")
            results.append(record)
            print(f"ReportsDataFile {report:24s} {sub:8s} HTTP {response.status_code} {len(body):>9,}B "
                  f"{'ZIP ' + str(record.get('csv_rows')) if record.get('members') else ''}")
            time.sleep(0.5)
    return results


def try_direct(session: curl.Session) -> list[dict]:
    results = []
    for url in DIRECT_URLS:
        try:
            response = session.get(url, timeout=45)
        except Exception as exc:  # noqa: BLE001
            results.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            print(f"direct {url.split('/')[-1][:52]:54s} ERROR {type(exc).__name__}")
            continue
        body = response.content
        record = {"url": url, "status": response.status_code, "bytes": len(body)}
        if response.status_code == 200:
            name = "direct_" + url.split("/")[-1].replace("/", "_")
            (RAW / name).write_bytes(body)
            record["saved"] = name
            if body[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    record["members"] = archive.namelist()
                    text = archive.read(archive.namelist()[0]).decode("utf-8", "replace").splitlines()
                    record["csv_header"] = text[0] if text else ""
                    record["csv_rows"] = len(text)
            else:
                record["head"] = body[:160].decode("utf-8", "replace")
        results.append(record)
        flag = "ZIP rows=" + str(record.get("csv_rows")) if record.get("members") else str(record.get("head", ""))[:60]
        print(f"direct {url.split('/')[-1][:52]:54s} HTTP {response.status_code} {len(body):>9,}B {flag}")
        time.sleep(0.4)
    return results


def main() -> int:
    session = curl.Session(impersonate="chrome124")
    for url in ("https://www.nseindia.com", "https://www.nseindia.com/all-reports"):
        session.get(url, timeout=30)
        time.sleep(1.0)

    results = {"reports_data_file": try_reports_data_file(session), "direct": try_direct(session)}
    OUT.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

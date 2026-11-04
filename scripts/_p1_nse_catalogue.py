"""P1 step 8b - read NSE's own report catalogue and find the pre-2024 archive.

Rather than guessing file names, scrape the All Reports page for the report
identifiers and archive URL patterns NSE itself publishes, then bisect the
oldest reachable derivatives bhavcopy date.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests as curl

RAW = Path("data/raw/p1_nse")
SESSION = curl.Session(impersonate="chrome124")

PAGES = [
    "https://www.nseindia.com/all-reports",
    "https://www.nseindia.com/all-reports-archive",
    "https://www.nseindia.com/static/all-reports-archive",
]

PATTERNS = [
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/fo_bhavcopy_{ymd}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/FO_BhavCopy_{ymd}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/fo_scriptwiseder_q_{ymd}.csv.zip",
]


def fetch_text(url: str) -> str:
    try:
        response = SESSION.get(url, timeout=40)
        print(f"GET {url} -> {response.status_code} {len(response.content):,}B")
        return response.text if response.status_code == 200 else ""
    except Exception as exc:  # noqa: BLE001
        print(f"GET {url} -> {type(exc).__name__}: {exc}")
        return ""


def main() -> int:
    SESSION.get("https://www.nseindia.com", timeout=30)
    time.sleep(1.0)

    catalogue: dict[str, object] = {}
    for page in PAGES:
        html = fetch_text(page)
        time.sleep(0.8)
        if not html:
            continue
        names = sorted(set(re.findall(r"[\"']([a-z0-9_]*bhavcopy[a-z0-9_]*)[\"']", html, re.I)))
        js_files = sorted(set(re.findall(r"src=\"([^\"]*\.js[^\"]*)\"", html)))
        report_refs = sorted(set(re.findall(r"reportname=([A-Za-z0-9_%-]+)", html)))
        api_refs = sorted(set(re.findall(r"(/[A-Za-z0-9_-]*[Rr]eports[A-Za-z0-9_/?=&-]*)", html)))[:40]
        catalogue[page] = {
            "bhavcopy_tokens": names,
            "reportname_params": report_refs,
            "report_paths": api_refs,
            "scripts": [j for j in js_files if "report" in j.lower() or "all" in j.lower()],
        }
        print("  bhavcopy tokens:", names[:20])
        print("  reportname params:", report_refs[:20])
        print("  report scripts:", catalogue[page]["scripts"][:6])

    (RAW / "all_reports_catalogue.json").write_text(json.dumps(catalogue, indent=2), encoding="utf-8")

    print("\n--- pattern reachability by date ---")
    dates = ["20260825", "20250130", "20240708", "20240125", "20240102", "20231228",
             "20231030", "20230727", "20230131", "20221229", "20220131", "20211230"]
    reach: dict[str, dict] = {}
    for pattern in PATTERNS:
        for ymd in dates:
            url = pattern.format(ymd=ymd)
            try:
                response = SESSION.get(url, timeout=25)
                status = response.status_code
            except Exception as exc:  # noqa: BLE001
                status = f"ERR {type(exc).__name__}"
                response = None
            entry = reach.setdefault(ymd, {})
            entry["OK" if status == 200 else "no"] = pattern
            if status == 200 and response is not None:
                (RAW / f"BhavCopy_NSE_FO_{ymd}.zip").write_bytes(response.content)
            time.sleep(0.15)
        print(f"pattern done: {pattern[:70]}")

    for ymd in dates:
        hit = reach[ymd].get("OK", "")
        print(f"{ymd}: {'REACHABLE' if hit else 'not reachable'}  {hit[:90]}")

    (RAW / "bhavcopy_reachability.json").write_text(json.dumps(reach, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

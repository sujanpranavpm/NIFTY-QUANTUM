"""P1 step 8e - last attempt at the pre-2024 legacy derivatives bhavcopy layout.

The ISO-20022 file name works from 2024-01-02 onwards.  Older dates use NSE's
previous naming convention, which used DDMMYYYY ordering.  This walks the
plausible legacy spellings so the reachable window is a measured fact.
"""

from __future__ import annotations

import json
import pathlib
import time

from curl_cffi import requests as curl

RAW = pathlib.Path("data/raw/p1_nse")
RAW.mkdir(parents=True, exist_ok=True)

DAY = {"dmy": "28122023", "dmy2": "28-12-2023", "ymd": "20231228"}

TEMPLATES = [
    "https://nsearchives.nseindia.com/content/fo/FO_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/fo_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/fo_bhavcopy_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/FO_Bhavcopy_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/FO_ScriptWiseDerivatives_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/FO_SecurityWiseDerivatives{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/security_wise_der_quote_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/archives/fo/FO_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2023/DEC/fo{dmy}.csv.zip",
    "https://archives.nseindia.com/content/fo/FO_{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{dmy}_F_0000.csv.zip",
    "https://nsearchives.nseindia.com/new/reportsdata/v3/fo/security/bhavcopy/2023/dec/fosecurity{dmy}.csv.zip",
]

# Dates whose official availability decides the reconciliation window; the HF
# cached expiry dates are included so reconciliation data is fetched in one pass.
CHECK_DAYS = [
    ("2026-11-03", "03112026", "20261103"),
    ("2026-08-04", "04082026", "20260804"),
    ("2026-02-24", "24022026", "20260224"),
    ("2025-09-30", "30092025", "20250930"),
    ("2025-09-02", "02092025", "20250902"),
    ("2025-03-27", "27032025", "20250327"),
    ("2025-01-30", "30012025", "20250130"),
    ("2024-12-26", "26122024", "20241226"),
    ("2024-08-29", "29082024", "20240829"),
    ("2024-07-25", "25072024", "20240725"),
    ("2024-01-25", "25012024", "20240125"),
    ("2024-01-02", "02012024", "20240102"),
    ("2023-12-28", "28122023", "20231228"),
    ("2023-11-30", "30112023", "20231130"),
    ("2023-06-22", "22062023", "20230622"),
    ("2022-12-30", "30122022", "20221230"),
    ("2022-09-29", "29092022", "20220929"),
    ("2021-12-30", "30122021", "20211230"),
    ("2021-05-27", "27052021", "20210527"),
]


def main() -> None:
    session = curl.Session(impersonate="chrome124")
    session.get("https://www.nseindia.com", timeout=30)
    time.sleep(1.0)

    print("=== legacy template sweep for 2023-12-28 ===")
    for template in TEMPLATES:
        url = template.format(**DAY)
        try:
            response = session.get(url, timeout=25)
        except Exception as exc:  # noqa: BLE001
            print(f"  {type(exc).__name__:18s} {url[-70:]}")
            continue
        print(f"  HTTP {response.status_code} {len(response.content):>9,}B  {url[-78:]}")
        if response.status_code == 200 and response.content[:2] == b"PK":
            name = "legacy_hit_" + url.split("/")[-1]
            (RAW / name).write_bytes(response.content)
            print(f"   >>> SAVED {name}")
        time.sleep(0.2)

    print("\n=== primary ISO name across boundary dates ===")
    hits = {}
    for iso, dmy, ymd in CHECK_DAYS:
        url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
        response = session.get(url, timeout=40)
        ok = response.status_code == 200 and response.content[:2] == b"PK"
        if ok:
            (RAW / f"BhavCopy_NSE_FO_{ymd}.zip").write_bytes(response.content)
        hits[iso] = {"status": response.status_code, "bytes": len(response.content), "ok": ok}
        print(f"  {iso}  HTTP {response.status_code}  {len(response.content):>9,}B  {'SAVED' if ok else ''}")
        time.sleep(0.4)

    (RAW / "legacy_sweep.json").write_text(json.dumps(hits, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

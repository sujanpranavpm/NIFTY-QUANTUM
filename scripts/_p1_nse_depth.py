"""P1 step 8 - measure the real retention depth of the official NSE F&O bhavcopy.

Reconciliation only works for dates whose official report can actually be
downloaded for free, so the reachable date range must be measured rather than
assumed.  The new ISO-20022 file layout and the legacy layout are both tried.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

from curl_cffi import requests as curl

RAW = Path("data/raw/p1_nse")
RAW.mkdir(parents=True, exist_ok=True)

NEW = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
LEGACY = [
    "https://nsearchives.nseindia.com/content/fo/fo_bhavcopy_{dmy}.zip",
    "https://nsearchives.nseindia.com/content/fo/FO_Bhavcopy_{dmy}.csv",
    "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mmm}/fo{dmy}.csv.zip",
    "https://nsearchives.nseindia.com/content/fo/fo_security_wise_quote_{dmy}.csv",
]

DATES = [
    date(2026, 8, 25), date(2026, 2, 24), date(2025, 9, 30), date(2025, 3, 27),
    date(2025, 1, 30), date(2024, 12, 26), date(2024, 8, 29), date(2024, 1, 25),
    date(2023, 6, 22), date(2022, 9, 29), date(2021, 12, 30), date(2021, 5, 27),
    date(2021, 1, 4), date(2019, 1, 1),
]


def probe(session: curl.Session, url: str, name: str) -> dict:
    try:
        response = session.get(url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"status": f"ERR {type(exc).__name__}", "bytes": 0}
    record = {"status": response.status_code, "bytes": len(response.content)}
    if response.status_code == 200 and response.content[:2] == b"PK":
        (RAW / name).write_bytes(response.content)
        record["saved"] = name
    return record


def main() -> int:
    session = curl.Session(impersonate="chrome124")
    session.get("https://www.nseindia.com", timeout=30)
    time.sleep(1.0)

    results = []
    for day in DATES:
        ymd = day.strftime("%Y%m%d")
        dmy = day.strftime("%d%m%Y")
        row = {"date": str(day), "new": None, "legacy_hits": []}
        got = probe(session, NEW.format(ymd=ymd), f"BhavCopy_NSE_FO_{ymd}.zip")
        row["new"] = got
        time.sleep(0.3)
        if not got.get("saved"):
            for template in LEGACY:
                url = template.format(
                    dmy=dmy, ymd=ymd, yyyy=day.strftime("%Y"), mmm=day.strftime("%b").upper()
                )
                name = url.split("/")[-1]
                alt = probe(session, url, name)
                time.sleep(0.2)
                if alt.get("saved"):
                    row["legacy_hits"].append({"url": url, **alt})
                    break
                row.setdefault("legacy_status", []).append(alt["status"])
        results.append(row)
        saved = bool(row["new"].get("saved")) or bool(row["legacy_hits"])
        print(f"{day}  new={row['new']['status']}/{row['new']['bytes']:,}B  "
              f"legacy={'hit' if row['legacy_hits'] else row.get('legacy_status')}  usable={saved}")

    Path("logs/p1_nse_bhav_depth.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("\nwrote logs/p1_nse_bhav_depth.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""P1 step 8d - query NSE's own report catalogue endpoints.

all-reports.js reveals the real discovery API:
  GET /api/daily-reports?key=FO
  GET /api/monthly-reports?key=FO

Dumping those gives the authoritative list of official derivatives reports and
the download link template for each, including any archive that reaches back
before 2024.
"""

from __future__ import annotations

import json
import pathlib
import time

from curl_cffi import requests as curl

OUT = pathlib.Path("data/raw/p1_nse")
OUT.mkdir(parents=True, exist_ok=True)

session = curl.Session(impersonate="chrome124")
session.get("https://www.nseindia.com", timeout=30)
time.sleep(1.0)
session.get("https://www.nseindia.com/all-reports", timeout=30)
time.sleep(1.0)

found = {}
for kind in ("daily-reports", "monthly-reports", "all-reports-archive", "archived-reports"):
    for key in ("FO", "EQ", "INDEX"):
        url = f"https://www.nseindia.com/api/{kind}?key={key}"
        try:
            response = session.get(url, timeout=40)
        except Exception as exc:  # noqa: BLE001
            print(f"{kind:20s} {key:6s} ERR {type(exc).__name__}")
            continue
        print(f"{kind:20s} {key:6s} HTTP {response.status_code} {len(response.content):,}B")
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            continue
        found[f"{kind}:{key}"] = payload
        (OUT / f"catalogue_{kind}_{key}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        time.sleep(0.6)

# Pull out anything derivatives related so the link templates are visible.
print("\n=== FO report entries mentioning bhavcopy / derivative / quote ===")
for name, payload in found.items():
    if not name.endswith("FO"):
        continue
    rows = payload if isinstance(payload, list) else payload.get("records") or payload.get("data") or []
    print(f"\n{name}: {len(rows)} rows; first row keys = {list(rows[0]) if rows else 'n/a'}")
    for row in rows or []:
        blob = json.dumps(row, default=str)
        if any(token in blob.lower() for token in ("bhavcopy", "scriptwise", "security_wise", "derivative", "oi", "option")):
            print("  ", blob[:300])

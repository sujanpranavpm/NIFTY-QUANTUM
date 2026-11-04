"""P1 step 7d - parameter sweep on NSE historical derivatives OTC endpoint.

The endpoint answered HTTP 200 but with zero records for a 2024 expiry, so the
retention window and the exact query contract have to be measured, not assumed.
Free, public, credential-free.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests as curl

BASE = "https://www.nseindia.com/api/historicalOR/fo/derivatives"
RAW = Path("data/raw/p1_nse")
RAW.mkdir(parents=True, exist_ok=True)

CASES = [
    # (label, params)
    ("2026 Aug monthly CE", {"from": "01-08-2026", "to": "25-08-2026", "optionType": "CE", "expiryDate": "25-08-2026", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
    ("2026 Sep monthly CE", {"from": "01-09-2026", "to": "29-09-2026", "optionType": "CE", "expiryDate": "29-09-2026", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
    ("2026 Oct monthly CE", {"from": "01-10-2026", "to": "27-10-2026", "optionType": "CE", "expiryDate": "27-10-2026", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
    ("2026 Oct weekly CE", {"from": "01-10-2026", "to": "06-10-2026", "optionType": "CE", "expiryDate": "06-10-2026", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
    ("2025 Sep monthly CE", {"from": "01-09-2025", "to": "30-09-2025", "optionType": "CE", "expiryDate": "30-09-2025", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
    ("2025 Jan monthly CE", {"from": "01-01-2025", "to": "30-01-2025", "optionType": "CE", "expiryDate": "30-01-2025", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
    ("2024 Aug monthly CE", {"from": "01-08-2024", "to": "29-08-2024", "optionType": "CE", "expiryDate": "29-08-2024", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
    ("2026 Oct FUT", {"from": "01-10-2026", "to": "27-10-2026", "optionType": "FUT", "expiryDate": "27-10-2026", "instrumentType": "FUTIDX", "symbol": "NIFTY"}),
    ("2026 Oct monthly CE strike", {"from": "25-08-2026", "to": "25-08-2026", "optionType": "CE", "strikePrice": "25000.00", "expiryDate": "25-08-2026", "instrumentType": "OPTIDX", "symbol": "NIFTY"}),
]


def main() -> int:
    session = curl.Session(impersonate="chrome124")
    for url in ("https://www.nseindia.com", "https://www.nseindia.com/products/reports"):
        session.get(url, timeout=30)
        time.sleep(1.0)

    results = []
    for label, params in CASES:
        query = {"strikePrice": "", **params}
        url = f"{BASE}?{urllib.parse.urlencode(query)}"
        try:
            response = session.get(url, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"{label:28s} ERROR {type(exc).__name__}: {exc}")
            results.append({"label": label, "url": url, "error": str(exc)})
            continue
        text = response.text or ""
        n = None
        record = {"label": label, "url": url, "status": response.status_code, "chars": len(text)}
        try:
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else payload
            n = len(rows or [])
            record["n_records"] = n
            if n:
                first = rows[0]
                record["record_keys"] = list(first)
                value = first.get("value", first)
                record["value_keys"] = list(value)[:30]
                record["sample"] = json.dumps(first, default=str)[:900]
                (RAW / f"otc_{label.replace(' ', '_')}.json").write_text(
                    json.dumps(payload, indent=2, default=str), encoding="utf-8"
                )
        except Exception as exc:  # noqa: BLE001
            record["not_json"] = f"{type(exc).__name__}: {text[:150]}"
        results.append(record)
        print(f"{label:28s} HTTP {response.status_code}  records={n}  chars={len(text):,}")
        time.sleep(1.2)

    Path("logs/p1_nse_otc_sweep.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("\nwrote logs/p1_nse_otc_sweep.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

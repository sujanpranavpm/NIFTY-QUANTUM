"""P1 step 7c - official NSE access using a browser-impersonating session.

Plain urllib gets HTTP 404 from NSE's Akamai edge for every JSON endpoint, so
this uses curl_cffi (already installed, free, open source) to present a real
Chrome TLS fingerprint and to warm up the cookie jar from the public pages,
exactly the way the maintained open-source NSE clients do.

Every call is to a free, public, credential-free NSE resource.  Results are
written to logs/p1_nse_official.json so later stages reuse measurements
instead of re-hitting the site.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from curl_cffi import requests as curl

OUT = Path("logs/p1_nse_official.json")
RAW = Path("data/raw/p1_nse")
RAW.mkdir(parents=True, exist_ok=True)

# NIFTY monthly-expiry Thursday that also exists as an HF archive file.
EXPIRY = "29-08-2024"

SESSION = curl.Session(impersonate="chrome124")


def warm() -> None:
    for url in ("https://www.nseindia.com", "https://www.nseindia.com/option-chain"):
        try:
            response = SESSION.get(url, timeout=30)
            print(f"warm {url} -> {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"warm {url} -> {type(exc).__name__}: {exc}")
        time.sleep(1.0)


def get_json(label: str, url: str) -> dict:
    try:
        response = SESSION.get(url, timeout=45)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "url": url, "error": f"{type(exc).__name__}: {exc}"}
    text = response.text or ""
    record = {"label": label, "url": url, "status": response.status_code, "chars": len(text)}
    if response.status_code == 200:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                record["top_keys"] = list(payload)[:12]
                data = payload.get("data")
                if isinstance(data, list):
                    record["n_records"] = len(data)
                    if data:
                        record["first_value_keys"] = list(data[0].get("value", {}))[:20]
                        record["sample"] = json.dumps(data[0], default=str)[:600]
            elif isinstance(payload, list):
                record["n_records"] = len(payload)
                record["sample"] = json.dumps(payload[0], default=str)[:600] if payload else ""
        except Exception as exc:  # noqa: BLE001
            record["not_json"] = f"{type(exc).__name__}: {text[:120]}"
    else:
        record["body_head"] = text[:160].replace("\n", " ")
    return record


def get_file(label: str, url: str, name: str) -> dict:
    try:
        response = SESSION.get(url, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "url": url, "error": f"{type(exc).__name__}: {exc}"}
    record = {"label": label, "url": url, "status": response.status_code, "bytes": len(response.content)}
    if response.status_code == 200:
        path = RAW / name
        path.write_bytes(response.content)
        record["saved"] = str(path)
        head = response.content[:400].decode("utf-8", "replace")
        record["head"] = head.replace("\r", "")[:400]
    return record


def main() -> int:
    warm()
    results = []

    urls = [
        (
            "histOTC derivatives NIFTY CE 29-08-2024",
            "https://www.nseindia.com/api/historicalOR/fo/derivatives"
            f"?from=01-08-2024&to={EXPIRY}&optionType=CE&strikePrice=&expiryDate={EXPIRY}"
            "&instrumentType=OPTIDX&symbol=NIFTY",
        ),
        (
            "all-reports-derivatives page",
            "https://www.nseindia.com/all-reports",
        ),
        (
            "contract specs page",
            "https://www.nseindia.com/static/products-services/equity-derivatives-contract-specifications",
        ),
        (
            "option chain live",
            "https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY",
        ),
        (
            "newspaper publication dates",
            "https://www.nseindia.com/api/equity-marketnewspaperpublication",
        ),
    ]
    for label, url in urls:
        record = get_json(label, url)
        results.append(record)
        print(f"{label:44s} {record.get('status') or record.get('error')}")
        time.sleep(0.8)

    files = [
        ("lot sizes (fo_mktlots)", "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", "fo_mktlots.csv"),
        ("current list derivatives", "https://nsearchives.nseindia.com/content/fo/fo_list_equity+derivatives.csv", "fo_list.csv"),
        ("circular FAOP68747", "https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf", "FAOP68747.pdf"),
    ]
    for label, url, name in files:
        record = get_file(label, url, name)
        results.append(record)
        print(f"{label:44s} {record.get('status') or record.get('error')} bytes={record.get('bytes', 0):,}")
        time.sleep(0.5)

    OUT.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

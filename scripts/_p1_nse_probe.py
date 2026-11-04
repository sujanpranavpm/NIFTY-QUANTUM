"""P1 step 7 - can we reach the official NSE reports at all, for free?

The reconciliation stage depends on downloading NSE daily derivatives
bhavcopy / security-wise quotes.  NSE blocks non-browser clients, so this
probe measures actual accessibility before any reconciliation is promised.

Zero cost, no credentials, no stored cookies beyond a live session.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

TARGETS = [
    ("archives static bhavcopy", "https://nsearchives.nseindia.com/content/fo/fo_bhavcopy_29092025.csv.gz"),
    ("archives all-reports", "https://nsearchives.nseindia.com/content/fo/FO_Block_BasicSIP_29092025.csv"),
    ("www all-reports json", "https://www.nseindia.com/api/all-reports-derivatives?type=equityDerivative"),
    ("www eqbhavcopyjson", "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050"),
    ("archives circular pdf", "https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf"),
    ("archives cm_ghh", "https://nsearchives.nseindia.com/cm/cm_ghh/2025-2026/Trading_Halt_Listing_20260101.PDF"),
    ("mse bse quote", "https://api.bseindia.com/BseIndiaAPI/api/getScripDashboardData/w"),
    ("huggingface api", "https://huggingface.co/api/datasets/thetrademarkk/india-index-options-1m"),
    ("cloudtraderpro", "https://cloudtraderpro.in/"),
]


def probe(url: str) -> dict:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45) as response:
            body = response.read()
            return {
                "url": url,
                "status": response.status,
                "bytes": len(body),
                "content_type": response.headers.get("Content-Type"),
                "head": body[:120].decode("utf-8", "replace").replace("\n", " ")[:120],
            }
    except urllib.error.HTTPError as exc:
        return {"url": url, "error": f"HTTP {exc.code}", "body_head": exc.read()[:160].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001 - probe reports whatever happens
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    results = []
    for label, url in TARGETS:
        result = probe(url)
        result["label"] = label
        results.append(result)
        status = result.get("status") or result.get("error")
        print(f"{label:26s} {str(status):12s} bytes={result.get('bytes', 0):>9,} {result.get('head', '')[:80]}")

    with open("logs/p1_nse_reachability.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print("\nwrote logs/p1_nse_reachability.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

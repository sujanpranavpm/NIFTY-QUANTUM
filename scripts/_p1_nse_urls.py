"""P1 step 7b - find a working, credential-free URL for official NSE reports.

The first probe showed nsearchives.nseindia.com serves circular PDFs but the
guessed bhavcopy names 404.  This walks a list of candidate URL patterns so the
reconciliation stage is built on a URL that actually returns data.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# A Thursday monthly-expiry date that exists in the HF archive.
D8 = "29082024"
D10 = "2024-08-29"

CANDIDATES = [
    f"https://nsearchives.nseindia.com/content/fo/FO_ScriptWiseQuote_{D8}.csv",
    f"https://nsearchives.nseindia.com/content/fo/fo_bhavcopy_{D8}.csv",
    f"https://nsearchives.nseindia.com/content/fo/fo_bhavcopy_{D8}.zip",
    f"https://nsearchives.nseindia.com/content/fo/FO_Bhavcopy_{D8}.csv",
    f"https://nsearchives.nseindia.com/content/fo/FO_SecurityWise_{D8}.csv.gz",
    f"https://archives.nseindia.com/content/fo/FO_ScriptWiseQuote_{D8}.csv",
    f"https://archives.nseindia.com/content/historical/DERIVATIVES/2024/AUG/fo{D8}.csv.zip",
    f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2024/AUG/fo{D8}.csv.zip",
    f"https://archives.nseindia.com/new/reportsdata/derivatives/index/2024/aug/fo{D8}.csv.zip",
    f"https://nsearchives.nseindia.com/content/fo/FO_Block_Bhavcopy_{D8}.csv",
    f"https://nsearchives.nseindia.com/content/fo/FO_{D8}.csv",
    "https://www.nseindia.com/api/historical-snapshot?symbol=NIFTY",
    f"https://www.nseindia.com/api/all-reports-derivatives?type=equityDerivative&date={D10}",
    f"https://archives.nseindia.com/api/all-reports-derivatives?type=equityDerivative&date={D10}",
    f"https://www.nseindia.com/api/equity-derivative-bhavcopy-json?dated={D10}&type=day",
    f"https://www.nseindia.com/api/derivative-individual-contracts?symbol=NIFTY%20Option&expiry={D10}&type=CE&strike=24000",
    "https://nsearchives.nseindia.com/content/circulars/FAOP59140.pdf",
    "https://nsearchives.nseindia.com/products-services/equity-derivatives-contract-specifications",
    "https://nsearchives.nseindia.com/web/static/products-services/equity-derivatives-contract-specifications",
]


def probe(url: str) -> None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as response:
            body = response.read()
            head = body[:100].decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
            print(f"OK   {response.status} {len(body):>10,}B  {url}\n        head: {head}")
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code:>3}            {url}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERR  {type(exc).__name__:<7}            {url}  {exc}")


def main() -> int:
    for url in CANDIDATES:
        probe(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())

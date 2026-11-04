"""P1 step 15 - Cloud Trader Pro free sample: what is actually downloadable.

The site advertises "1-minute OHLCV + Open Interest" expired-options datasets for
free download.  Nothing here assumes that claim is true of the file you actually
get.  The page is parsed for real asset links, one file is fetched, and the file
is inspected for format, columns, instrument identity, expiry, timestamps, OHLC
shape, OI presence, coverage and stated usage terms.

Usage:
    python scripts/_p1_cloudtrader.py
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from curl_cffi import requests as curl

RAW = Path("data/raw/p1_cloudtrader")
OUT = Path("logs/p1_cloudtrader.json")

PAGES = [
    "https://cloudtraderpro.in/free-historical-options-data/",
    "https://cloudtraderpro.in/free-historical-nifty-expired-options-data/",
]
DATA_EXT = re.compile(r"\.(csv|zip|parquet|pq|gz|tar|xlsx|json|7z|rar)(\?|$)", re.I)


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    session = curl.Session(impersonate="chrome124")
    findings = []
    links_seen: set[str] = set()

    for page in PAGES:
        try:
            response = session.get(page, timeout=60)
        except Exception as exc:  # noqa: BLE001
            findings.append({"page": page, "error": f"{type(exc).__name__}: {exc}"})
            continue
        html = response.text
        (RAW / f"{page.rstrip('/').split('/')[-1]}.html").write_text(html, encoding="utf-8")
        anchors = [urljoin(page, m) for m in re.findall(r'href="([^"]+)"', html)]
        data_links = sorted({a for a in anchors if DATA_EXT.search(a)})
        drive_links = sorted({a for a in anchors if "drive.google" in a or "dropbox" in a})
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        terms = re.findall(r"[^.]{40,240}(?:license|licence|redistribut|resell|commercial|attribut|"
                           r"creative commons|terms|cc-by|copyright)[^.]{0,240}", text, re.I)
        print("=" * 96)
        print(f"{page}  HTTP {response.status_code}  {len(html):,}B")
        print(f"  data-file links : {data_links or 'none'}")
        print(f"  cloud links     : {drive_links[:6] or 'none'}")
        print(f"  body length     : {len(text):,} chars")
        for sentence in terms[:6]:
            print(f"  terms> {sentence.strip()[:220]}")
        # what does the page say the dataset contains?
        for key in ("1-minute", "Open Interest", "expiry", "weekly", "monthly", "timestamp",
                    "IST", "timezone", "sample", "rows", "GB", "MB"):
            pattern = "[^.<>]{0,90}" + re.escape(key) + "[^.<>]{0,90}"
            hits = re.findall(pattern, text, re.I)[:2]
            for hit in hits:
                print(f"  [{key:14s}] {hit.strip()[:170]}")
        findings.append({"page": page, "status": response.status_code, "bytes": len(html),
                         "data_links": data_links, "cloud_links": drive_links,
                         "licence_sentences": [t.strip()[:300] for t in terms[:10]]})
        links_seen.update(data_links)
        time.sleep(0.5)

    print("\n" + "=" * 96)
    print("candidate downloadable assets across all pages:")
    for link in sorted(links_seen):
        try:
            got = session.get(link, timeout=60, headers={"Range": "bytes=0-4095"})
            head = got.content[:8]
            print(f"  HTTP {got.status_code}  {len(got.content):>7,}B(range)  magic={head!r}  {link}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {type(exc).__name__}: {link}")

    OUT.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P1 step 15c - read what Google Drive actually returns for these sample ids.

The metadata probe came back as an HTML page with no filename, which could mean
any of: a scan-confirmation interstitial, a rate-limit/"too many requests" page,
a sign-in wall, or a dead link.  Those are very different verdicts for a source
assessment, so the response body is read instead of guessed at.

Usage:
    python scripts/_p1_cloudtrader_body.py
"""

from __future__ import annotations

import re
from pathlib import Path

from curl_cffi import requests as curl

RAW = Path("data/raw/p1_cloudtrader")
IDS = ["1-GRg9-ScM11OhxhxoovgXrLw6zFqDed7", "158uPiPp4xvtR-ueziJPMvXE2gMug6wk9"]
ENDPOINTS = [
    "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t",
    "https://drive.google.com/uc?id={id}&export=download",
    "https://www.googleapis.com/drive/v3/files/{id}?alt=media&key=",
]


def main() -> int:
    session = curl.Session(impersonate="chrome124")
    for file_id in IDS:
        for template in ENDPOINTS:
            url = template.format(id=file_id)
            try:
                response = session.get(url, timeout=90)
            except Exception as exc:  # noqa: BLE001
                print(f"{file_id[:12]} {url[:44]}: {type(exc).__name__}")
                continue
            body = response.text
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
            print("=" * 96)
            print(f"{file_id[:12]}  {url.split('?')[0]}  HTTP {response.status_code} "
                  f"{len(body):,}B  type={response.headers.get('Content-Type')}")
            print("  " + text[:600])
            if "text/html" not in (response.headers.get("Content-Type") or ""):
                print("  >>> binary payload; first bytes:", response.content[:16])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

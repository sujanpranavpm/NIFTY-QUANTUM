"""P1 step 15d - is the Cloud Trader Pro sample still publicly shared?

drive.usercontent returned a sign-in interstitial, which is ambiguous between
"a public file that throttles anonymous clients" and "sharing was turned off".
The viewer and embed endpoints report those two states differently, so both are
read for one file with and without a browser profile.

Usage:
    python scripts/_p1_cloudtrader_share.py
"""

from __future__ import annotations

import re
from pathlib import Path

from curl_cffi import requests as curl

RAW = Path("data/raw/p1_cloudtrader")
IDS = [
    "1-GRg9-ScM11OhxhxoovgXrLw6zFqDed7",
    "158uPiPp4xvtR-ueziJPMvXE2gMug6wk9",
]
Signals = [
    "File does not exist", "Unable to find the file", "Request had insufficient",
    "MissingPermissionOrShareRateLimit", "Sign in", "Download", "getAsync",
]


def strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def main() -> int:
    for label, session in (("impersonated", curl.Session(impersonate="chrome124")),
                          ("plain-urllib", curl.Session())):
        for file_id in IDS:
            for n, url in enumerate((
                        f"https://drive.google.com/file/d/{file_id}/view",
                        f"https://drive.google.com/file/d/{file_id}/view?bshid=867455646577&authuser=0",
                        f"https://drive.google.com/u/0/uc?id={file_id}&export=download&confirm=t")):
                try:
                    response = session.get(url, timeout=60)
                except Exception as exc:  # noqa: BLE001
                    print(f"{label:14s} {file_id[:10]} HTTP-ERR {type(exc).__name__}")
                    continue
                body = strip(response.text)
                hits = [s for s in Signals if isinstance(s, str) and s.lower() in body.lower()]
                print("=" * 90)
                print(f"{label:14s} {file_id[:10]} HTTP {response.status_code} "
                      f"{len(response.text):,}B {url.split('?')[0].split('.google.com')[1][:34]}")
                print(f"   signals: {hits}")
                print(f"   text: {body[:300]}")
                safe = re.sub(r"[^A-Za-z0-9_-]", "", url.split("?")[0].split("/")[-1]) or "root"
                (RAW / f"share_{label}_{file_id[:10]}_{n}_{safe}.txt").write_text(
                    body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

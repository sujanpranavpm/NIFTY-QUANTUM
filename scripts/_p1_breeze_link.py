"""P1 step 16b - find the security-master link the Breeze reference actually points to.

The guessed hosts failed DNS, so the link is read from the documentation page
itself instead of assumed.
"""

from __future__ import annotations

import re
from pathlib import Path

from curl_cffi import requests as curl

RAW = Path("data/raw/p1_breeze")
PAGES = [
    "https://api.icicidirect.com/breezeapi/documents/index.html",
    "https://api.icicidirect.com/breezeapi/documents/Breeze_API_Docs.md",
]


def main() -> int:
    session = curl.Session(impersonate="chrome124")
    for page in PAGES:
        try:
            html = session.get(page, timeout=90).text
        except Exception as exc:  # noqa: BLE001
            print(f"{page}: {type(exc).__name__}")
            continue
        RAW.joinpath("documents_index.html").write_text(html, encoding="utf-8")
        print("=" * 90)
        print(f"{page}  {len(html):,}B")
        links = sorted({m for m in re.findall(r'href="([^"]+)"', html)
                        if re.search(r"master|\.zip|\.csv|\.xls|security", m, re.I)})
        print(f"  candidate links: {links[:20] or 'none'}")
        for hit in re.findall(r"[^\"'<>]{0,60}[Ss]ecurity ?[Mm]aster[^\"'<>]{0,120}", html)[:8]:
            print(f"  context> {hit.strip()}")
        # docsify-style loading of markdown sources
        for hit in re.findall(r"(?:loadSidebar|loadMarkdown|alias|homepage)[^\n]{0,160}", html)[:8]:
            print(f"  spa>   {hit.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

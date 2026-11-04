"""Extract the price-step and strike-interval rows from NSE's own specification
page text, plus the linked strike-interval circular if reachable."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from curl_cffi import requests as curl

TEXT = Path("data/raw/p1_nse/contract_specs.html")


def main() -> int:
    session = curl.Session(impersonate="chrome124")
    session.get("https://www.nseindia.com", timeout=30)
    url = ("https://www.nseindia.com/static/products-services/"
           "equity-derivatives-contract-specifications")
    response = session.get(url, timeout=60)
    TEXT.write_bytes(response.content)
    html = re.sub(r"<[^>]+>", "|", response.text)
    html = re.sub(r"(&nbsp;|\|{2,}|\s{2,})", "|", html)

    for marker in ("Price Steps", "Strike Price Intervals", "Lot/Face"):
        for match in re.finditer(marker, html):
            window = html[match.start():match.start() + 420]
            print("=" * 90)
            print(marker, "->", re.sub(r"\|+", " | ", window)[:420])
            break

    links = re.findall(r'href="([^"]+)"[^>]*>[^<]*Strike Price Interval', response.text, re.I)
    print("\nstrike-interval links:", links)
    for href in links[:3]:
        absolute = href if href.startswith("http") else "https://www.nseindia.com" + href
        got = session.get(absolute, timeout=60)
        print(f"  {absolute} -> HTTP {got.status_code} {len(got.content):,}B")
        if got.status_code == 200 and got.content[:4] == b"%PDF":
            from pypdf import PdfReader

            import io

            reader = PdfReader(io.BytesIO(got.content))
            text = "".join(p.extract_text() or "" for p in reader.pages)
            name = Path(absolute).name
            Path("data/raw/p1_nse/circulars").mkdir(parents=True, exist_ok=True)
            (Path("data/raw/p1_nse/circulars") / name).write_bytes(got.content)
            print("---- extracted text ----")
            print(re.sub(r"\s{2,}", " ", text)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

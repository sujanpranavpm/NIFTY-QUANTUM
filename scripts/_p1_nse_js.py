"""P1 step 8c - read NSE's own all-reports.js to learn the real archive contract.

The public All Reports page is driven by this script; it contains the report
keys and the download endpoint the site itself uses.  Extracting them is the
only reliable way to find the pre-2024 derivatives archive, instead of guessing
file names.
"""

from __future__ import annotations

import pathlib
import re
import time

from curl_cffi import requests as curl

OUT = pathlib.Path("data/raw/p1_nse")
OUT.mkdir(parents=True, exist_ok=True)

session = curl.Session(impersonate="chrome124")
session.get("https://www.nseindia.com", timeout=30)
time.sleep(1.0)

for version in ("v=30102026", ""):
    url = "https://www.nseindia.com/dist/js/sections/all-reports.js" + (f"?{version}" if version else "")
    response = session.get(url, timeout=40)
    print(url, "->", response.status_code, f"{len(response.content):,}B")
    if response.status_code == 200:
        break

text = response.text
(OUT / "all-reports.js").write_text(text, encoding="utf-8")

print("\n--- download endpoints ---")
for match in sorted(set(re.findall(r"[\"'`]([^\"'`]*(?:reportsdata|ReportsDataFile|archive|Archive|bhavcopy|BhavCopy)[^\"'`]*)[\"'`]", text)))[:60]:
    print(" ", match[:180])

print("\n--- url templates in file ---")
for match in sorted(set(re.findall(r"https?://[^\s\"'`+]+", text)))[:40]:
    print(" ", match[:180])

print("\n--- reportKey style tokens ---")
tokens = sorted(set(re.findall(r"([a-z][a-z0-9_]{6,}bhavcopy[a-z0-9_]*|fo_[a-z0-9_]{4,}|[a-z]*dei[a-z0-9_]{3,})", text)))
print(" ", tokens[:80])

print("\n--- object entries mentioning derivatives ---")
for match in re.findall(r"\{[^{}]{0,220}?derivatives[^{}]{0,220}?\}", text, re.I)[:10]:
    print(" ", match[:260])

"""P1 step 11 - read the primary NSE circulars instead of trusting recollection.

An earlier pass at the expiry calendar tested the archive against a regime table
written from memory.  That table was wrong, so this step extracts the text of the
actual NSE circulars and lets the circulars define the expected regime.

Usage:
    python scripts/_p1_nse_circulars.py
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from curl_cffi import requests as curl
from pypdf import PdfReader

RAW = Path("data/raw/p1_nse/circulars")
OUT = Path("logs/p1_nse_circulars.json")

# Candidate circular numbers found by web search over NSE's public archive.
CIRCULARS = [
    "FAOP54334",  # 2022-11-07 - weekly expiry pause Dec 2022 / Jan 2023
    "FAOP64506",  # 2024-10-10 - one benchmark index with weekly expiry from 2024-11-20
    "FAOP68747",  # 2025-06-25 - weekly expiry day moved to Tuesday from 2025-09-01
]

HOSTS = [
    "https://nsearchives.nseindia.com/content/circulars/",
    "https://archives.nseindia.com/content/circulars/",
]

SENTENCE = re.compile(r"[^.\n]*(?:weekly|monthly|expiry|last Thursday|Tuesday|benchmark index)[^.\n]*\.", re.I)


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    session = curl.Session(impersonate="chrome124")
    session.get("https://www.nseindia.com", timeout=30)
    time.sleep(1.0)

    findings = []
    for number in CIRCULARS:
        pdf = RAW / f"{number}.pdf"
        if not pdf.exists() or pdf.stat().st_size == 0:
            for host in HOSTS:
                response = session.get(f"{host}{number}.pdf", timeout=60)
                if response.status_code == 200 and response.content[:4] == b"%PDF":
                    pdf.write_bytes(response.content)
                    print(f"{number}: fetched from {host} ({len(response.content):,}B)")
                    break
                print(f"{number}: {host} -> HTTP {response.status_code}")
            else:
                print(f"{number}: NOT REACHABLE")
                continue
        else:
            print(f"{number}: already cached")

        text = "".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
        text = re.sub(r"[ \t]+", " ", text)
        sentences = [s.strip() for s in SENTENCE.findall(text) if len(s.strip()) > 40]
        print("-" * 90)
        print(f"{number}  ({pdf.stat().st_size:,}B, {len(text)} chars of text)")
        for sentence in sentences[:14]:
            print("   * " + sentence.replace("\n", " ")[:240])
        findings.append({
            "circular": number,
            "bytes": pdf.stat().st_size,
            "text_chars": len(text),
            "full_text": text,
            "relevant_sentences": sentences,
        })
        time.sleep(0.5)

    OUT.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

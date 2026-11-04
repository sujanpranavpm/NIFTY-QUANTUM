"""P1 step 16 - Breeze: does the public security master even address expired contracts?

No API credentials are used and none are requested.  The only Breeze artifact that
is published without an account is the security master (the "token mapping" file
the reference links to), and it decides a real feasibility question: can a caller
name a NIFTY option contract that has already expired, which is what any
expired-options backtest needs?

Usage:
    python scripts/_p1_breeze_master.py
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pandas as pd
from curl_cffi import requests as curl

RAW = Path("data/raw/p1_breeze")
CANDIDATES = [
    "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip",
]


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    session = curl.Session(impersonate="chrome124")
    for url in CANDIDATES:
        try:
            head = session.get(url, timeout=60, headers={"Range": "bytes=0-3"})
        except Exception as exc:  # noqa: BLE001
            print(f"  {type(exc).__name__:22s} {url}")
            continue
        print(f"  HTTP {head.status_code} {head.headers.get('Content-Type')} "
              f"total={head.headers.get('Content-Range')} magic={head.content[:4]!r}  {url}")
        if head.status_code not in (200, 206) or head.content[:2] != b"PK":
            continue
        path = RAW / "security_master_csv.zip"
        if path.exists() and path.stat().st_size > 0:
            blob = path.read_bytes()   # resume: reuse the cached archive
            print(f"  reusing cached {path.name} {len(blob):,}B")
        else:
            blob = session.get(url, timeout=300).content
            path.write_bytes(blob)
            print(f"  saved {path.name} {len(blob):,}B")
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
            print(f"  entries: {names}")
            chosen = next((n for n in names if n.startswith("FONSE")), names[0])
            raw = archive.read(chosen).decode("utf-8", "replace")
            print(f"\n  chosen entry {chosen}, first line:\n    {raw.splitlines()[0][:400]}")
            first = raw.splitlines()[0]
            sep = max(["\t", ",", "|"], key=lambda c: first.count(c))
            print(f"  delimiter detected: {sep!r}  fields in header: {first.count(sep) + 1}")
            frame = pd.read_csv(io.StringIO(raw), sep=sep, dtype=str)
        print(f"\n  columns: {list(frame.columns)}")
        print(f"  rows: {len(frame):,}")
        frame.to_parquet(RAW / "security_master.parquet", index=False)

        keep = [c for c in frame.columns if re.search(
            r"^ExchangeCode$|^ShortName$|^ExpiryDate$|^StrikePrice$|^OptionType$|LotSize|TickSize|^Series$|DeleteFlag|ExpulsionDate|PermittedToTrade|^Token$",  # noqa: E501
            c, re.I)]
        print(f"  identity columns kept for analysis: {keep}")
        text_cols = {c: frame[c].astype(str) for c in keep}
        for col in keep:
            values = text_cols[col]
            print(f"   {col:26s} nunique={values.nunique():>8,}  "
                  f"sample={values.dropna().unique()[:4].tolist()}")

        nifty = frame[text_cols["ShortName"].str.strip().str.upper() == "NIFTY"] \
            if "ShortName" in text_cols else frame.iloc[:0]
        print(f"\n  rows with ShortName == NIFTY: {len(nifty):,}")
        if "ExpiryDate" in nifty.columns:
            exp = nifty["ExpiryDate"].astype(str).str.strip()
            days = pd.to_datetime(exp, format="%d-%b-%Y", errors="coerce").dt.date.dropna()
            print(f"  distinct NIFTY expiry dates: {days.nunique()}  "
                  f"range {days.min()} .. {days.max()}")
            today = pd.Timestamp.now().normalize().date()
            past = sorted(d for d in days.unique() if d < today)
            print(f"  expiries in the past still listed in the master: {len(past)}"
                  f"  (oldest {past[0] if past else '-'} newest {past[-1] if past else '-'})")
            print(f"  NIFTY strikes: {nifty['StrikePrice'].astype(str).nunique()} distinct"
                  f"  OptionType: {nifty['OptionType'].value_counts().to_dict()}")
            print(f"  NIFTY LOTSIZE values: {nifty['LotSize'].value_counts().to_dict()}")
            print(f"  NIFTY TickSize values: {nifty['TickSize'].value_counts().to_dict()}")
        for col in ("OptionType", "Series", "DeleteFlag", "PermittedToTrade"):
            if col in frame.columns:
                print(f"  {col}: {frame[col].value_counts().head(8).to_dict()}")
        return 0
    print("\n  no security master retrieved")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

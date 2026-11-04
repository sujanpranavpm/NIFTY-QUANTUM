"""P1 step 14 - historical contract reference data measured from official records.

The directive forbids hardcoding today's lot size, strike interval or tick size
across history.  All three are available from records already on disk or from
NSE's own public pages, so nothing has to be assumed:

  lot size        - NewBrdLotQty in each daily F&O bhavcopy
  strike interval - the spacing of StrkPric for a given expiry in the bhavcopy
  tick size       - NSE contract-specifications page, corroborated by the price
                    grid actually present in the 1-minute archive

Only cached bhavcopies are used, plus one fetch of the specifications page.

Usage:
    python scripts/_p1_reference_data.py
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT = Path("logs/p1_reference_data.json")
SPECS = Path("logs/p1_nse_contract_specs.txt")


def nifty_options(ymd: str) -> pd.DataFrame:
    path = NSE / f"BhavCopy_NSE_FO_{ymd}.zip"
    raw = zipfile.ZipFile(path)
    frame = pd.read_csv(io.StringIO(raw.read(raw.namelist()[0]).decode("utf-8", "replace")))
    opts = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")].copy()
    opts["XpryDt"] = pd.to_datetime(opts["XpryDt"]).dt.strftime("%Y-%m-%d")
    return opts


def strike_spacing(strikes: list[float]) -> int | None:
    arr = sorted({float(s) for s in strikes})
    if len(arr) < 3:
        return None
    gaps = [round(b - a, 4) for a, b in zip(arr, arr[1:])]
    return Counter(gaps).most_common(1)[0][0]


def from_bhavcopies() -> list[dict]:
    rows = []
    for path in sorted(NSE.glob("BhavCopy_NSE_FO_*.zip")):
        ymd = path.stem.split("_")[-1]
        if not ymd.isdigit() or len(ymd) != 8:
            continue
        try:
            opts = nifty_options(ymd)
        except Exception:  # noqa: BLE001
            continue
        if opts.empty:
            continue
        near = sorted(opts["XpryDt"].unique())[:1]  # nearest expiry only
        lots = Counter(int(x) for x in pd.to_numeric(opts["NewBrdLotQty"], errors="coerce").dropna())
        spacings = Counter()
        nearest_gaps: list[float] = []
        for expiry, group in opts.groupby("XpryDt"):
            step = strike_spacing(list(group["StrkPric"].dropna()))
            if step is not None:
                spacings[step] += 1
            if expiry == (near[0] if near else ""):
                arr = sorted({float(s) for s in group["StrkPric"].dropna()})
                nearest_gaps = sorted({round(b - a, 4) for a, b in zip(arr, arr[1:])})
        rows.append({
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
            "lot_sizes_all_expiries": dict(lots),
            "strike_interval_by_expiry_count": dict(spacings),
            "all_gaps_nearest_expiry": nearest_gaps,
            "nearest_expiry": near[0] if near else None,
            "strikes_nearest_expiry": int(opts[opts["XpryDt"] == (near[0] if near else "")]["StrkPric"].nunique()),
        })
    return rows


def from_specifications_page() -> str:
    """NSE's contract-specifications page states the tick size.  Requires the
    impersonating client; the plain client gets a 404 from the CDN."""
    from curl_cffi import requests as curl

    session = curl.Session(impersonate="chrome124")
    session.get("https://www.nseindia.com", timeout=30)
    session.get("https://www.nseindia.com/option-chain", timeout=30)
    url = ("https://www.nseindia.com/static/products-services/"
           "equity-derivatives-contract-specifications")
    response = session.get(url, timeout=60)
    text = re.sub(r"<[^>]+>", " ", response.text)
    text = re.sub(r"\s+", " ", text)
    hits = re.findall(r"[^.;]{0,160}(?:tick|lot size|strike|price grid|0\.05)[^.;]{0,160}", text, re.I)
    body = (f"HTTP {response.status_code}  {len(response.content):,}B\n"
            + "\n".join(f"  * {h.strip()[:200]}" for h in hits[:40]))
    SPECS.write_text(body, encoding="utf-8")
    return body


def from_hf_archive() -> list[dict]:
    """Corroborate the tick: which price grid does the archive actually use?"""
    rows = []
    for path in sorted(HF.glob("options__NIFTY__*.parquet"))[:6]:
        frame = pd.read_parquet(path, columns=["open", "high", "low", "close"])
        prices = pd.concat([frame[c] for c in frame.columns]).dropna().astype(float)
        prices = prices[prices > 0.005]
        cents = (prices * 100).round(0).astype("int64")
        rows.append({
            "file": path.name,
            "pct_on_5paise_grid": round(100.0 * float((cents % 5 == 0).mean()), 3),
            "pct_on_10paise_grid": round(100.0 * float((cents % 10 == 0).mean()), 3),
            "pct_off_grid": round(100.0 * float((cents % 5 != 0).mean()), 3),
        })
    return rows


def hf_strike_grid() -> list[dict]:
    """Does the archive use the same strike ladder as the exchange?"""
    rows = []
    for path in sorted(HF.glob("options__NIFTY__*.parquet")):
        strikes = pd.read_parquet(path, columns=["strike", "expiry"])
        out = []
        for expiry, group in strikes.groupby("expiry", observed=True):
            arr = sorted({float(s) for s in group["strike"].dropna()})
            if len(arr) < 3:
                continue
            gaps = Counter(round(b - a, 4) for a, b in zip(arr, arr[1:]))
            out.append({"expiry": expiry, "n": len(arr), "gaps": dict(gaps)})
        rows.append({"file": path.name, "expiries": out[:2]})
    return rows


def main() -> int:
    bhav = from_bhavcopies()
    frame = pd.DataFrame(bhav)
    print("=== NIFTY index options, official bhavcopies on disk ===")
    if not frame.empty:
        frame = frame.sort_values("date")
        with pd.option_context("display.width", 220, "display.max_columns", 10):
            print(frame.to_string(index=False))
        print("\nlot-size history (date -> lots seen):")
        for row in bhav:
            print(f"  {row['date']}  {row['lot_sizes_all_expiries']}")

    print("\n=== archive price grid (does it respect a 0.05 tick?) ===")
    grid = from_hf_archive()
    for row in grid:
        print(f"  {row['file']:44s} on5={row['pct_on_5paise_grid']}%  "
              f"on10={row['pct_on_10paise_grid']}%  off={row['pct_off_grid']}%")

    print("\n=== archive strike ladder, per expiry ===")
    for row in hf_strike_grid():
        print(f"  {row['file']:44s} {row['expiries']}")

    print("\n=== NSE contract specifications page ===")
    try:
        print(from_specifications_page()[:2000])
    except Exception as exc:  # noqa: BLE001
        print(f"  could not read: {type(exc).__name__}: {exc}")

    OUT.write_text(json.dumps({"bhavcopy_reference": bhav, "hf_price_grid": grid,
                               "hf_strike_ladder": hf_strike_grid()}, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

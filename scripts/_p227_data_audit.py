"""P2.2.7 read-only data audit: introspect every dataset that exists on disk.

This script NEVER writes to data/, artifacts/ or any dataset.  It reads file
schemas, row counts, date coverage and column names so the coverage audit is
built from the actual working tree, not from documentation or old progress
summaries.  Output goes to stdout (tee to logs/_p227_data_audit.log).
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def describe_parquet(path: Path) -> None:
    try:
        df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        print(f"  {path.name}: UNREADABLE ({exc})")
        return
    idx = str(df.index.name or type(df.index).__name__)
    rng = ""
    if isinstance(df.index, pd.DatetimeIndex) and len(df):
        rng = f" index {df.index.min()} .. {df.index.max()}"
    elif len(df):
        for col in ("timestamp", "date", "datetime"):
            if col in df.columns:
                rng = f" {col} {df[col].min()} .. {df[col].max()}"
                break
    print(f"  {path.name}: rows={len(df)} cols={list(df.columns)} idx={idx}{rng}")


# ---------------------------------------------------------------- raw options
hr("data/raw/p1_sample  (raw per-expiry option bhavcopy parquet)")
samp = sorted((ROOT / "data/raw/p1_sample").glob("*.parquet"))
print(f"  files={len(samp)}")
if samp:
    print(f"  first={samp[0].name}  last={samp[-1].name}")
    describe_parquet(samp[0])
    describe_parquet(samp[len(samp) // 2])
    describe_parquet(samp[-1])

# ------------------------------------------------------------- raw bhavcopy
hr("data/raw/p1_nse  (NSE FO daily bhavcopy zips)")
zips = sorted((ROOT / "data/raw/p1_nse").glob("BhavCopy_NSE_FO_*.zip"))
print(f"  bhavcopy zips={len(zips)}")
if zips:
    print(f"  first={zips[0].name}  last={zips[-1].name}")
    try:
        with zipfile.ZipFile(zips[0]) as z:
            inner = z.namelist()[0]
            with z.open(inner) as fh:
                head = fh.readline().decode("utf-8", "replace")
        print(f"  inner={inner}\n  header={head.strip()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not peek zip: {exc})")

# ---------------------------------------------------------------- cache/1d
hr("data/cache/1d  (index/futures daily cache)")
c1d = sorted((ROOT / "data/cache/1d").iterdir()) if (ROOT / "data/cache/1d").exists() else []
print(f"  files={len(c1d)}")
for f in c1d[:6]:
    if f.suffix == ".parquet":
        describe_parquet(f)
    else:
        print(f"  {f.name} ({f.stat().st_size} bytes)")

# --------------------------------------------------------- derived/candles
hr("data/derived/candles  (normalised intraday candle parquet)")
cd = sorted((ROOT / "data/derived/candles").glob("*.parquet"))
print(f"  files={len(cd)}")
# reveal filename structure
for f in cd[:5]:
    print(f"  e.g. {f.name}")
# group by resolution tag parsed from filename (…__<res>__.parquet heuristic)
from collections import Counter  # noqa: E402


def res_tag(name: str) -> str:
    parts = name.replace(".parquet", "").split("__")
    return parts[-1] if len(parts) > 1 else "?"


tags = Counter(res_tag(f.name) for f in cd)
print(f"  resolution/name-tag counts: {dict(tags)}")
for f in (cd[:2] + cd[-2:]):
    describe_parquet(f)

# ------------------------------------------------------------- reference csvs
hr("data/reference  (reference / coverage files)")
for f in sorted((ROOT / "data/reference").glob("*.csv")):
    try:
        df = pd.read_csv(f)
        print(f"\n  {f.name}: rows={len(df)} cols={list(df.columns)}")
        for dcol in ("expiry", "trading_day", "date", "effective_from"):
            if dcol in df.columns:
                try:
                    dt = pd.to_datetime(df[dcol], errors="coerce")
                    print(f"    {dcol}: {dt.min()} .. {dt.max()}")
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        print(f"  {f.name}: UNREADABLE ({exc})")

# coverage registry deep-dive
hr("data/reference/nifty_coverage_registry.csv  (deep dive)")
reg = pd.read_csv(ROOT / "data/reference/nifty_coverage_registry.csv")
print(f"  rows={len(reg)} cols={list(reg.columns)}")
for col in ("level", "status", "option_type", "arbiter", "expected", "observed"):
    if col in reg.columns:
        vc = reg[col].astype(str).value_counts().head(8)
        print(f"  {col}: {dict(vc)}")
if "expiry" in reg.columns:
    ex = reg.loc[reg.get("level") == "expiry", "expiry"] if "level" in reg.columns else reg["expiry"]
    ex = pd.to_datetime(ex, errors="coerce").dropna()
    print(f"  distinct expiries={ex.nunique()}  span={ex.min()} .. {ex.max()}")
if "trading_day" in reg.columns:
    td = pd.to_datetime(reg["trading_day"], errors="coerce").dropna()
    print(f"  trading_day span={td.min()} .. {td.max()}  n={td.nunique()}")

# ------------------------------------------------------------------ artifacts
hr("artifacts/p223,p224,p225,p226  (experiment coverage/summary JSON)")
for sub in ("p223", "p224", "p225", "p226"):
    d = ROOT / "artifacts" / sub
    if not d.exists():
        print(f"  {sub}: (absent)")
        continue
    for jf in sorted(list(d.glob("*.json")) + list(d.glob("*/*.json"))):
        try:
            import json
            payload = json.loads(jf.read_text(encoding="utf-8"))
            keys = list(payload)[:12]
            print(f"  {jf.relative_to(ROOT)}: top-keys={keys}")
            for k in ("resolution", "trades", "eligible_entries", "unique_entries",
                      "rejections", "exit_convention", "charge_as_of",
                      "total_charge_cost", "data_start", "data_end", "expiries",
                      "usable_options"):
                if isinstance(payload, dict) and k in payload:
                    v = payload[k]
                    if isinstance(v, dict):
                        v = dict(list(v.items())[:6])
                    print(f"      {k} = {v}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {jf.name}: (json read failed: {exc})")

# ----------------------------------------------------------------- snapshot
hr("nifty_quant/data/assets/nifty50_snapshot.csv")
snap = pd.read_csv(ROOT / "nifty_quant/data/assets/nifty50_snapshot.csv")
print(f"  rows={len(snap)} cols={list(snap.columns)}")
print(snap.head(3).to_string())

print("\nDone (read-only; nothing written).")

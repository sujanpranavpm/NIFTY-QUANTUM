"""Diagnostic: are expiry files pure, and what happened on the two outlier file-days?"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _p1_nse_reconcile import load_official  # noqa: E402

HF = Path("data/raw/p1_sample")
COLS = ["timestamp", "volume", "trading_day", "symbol", "strike", "option_type", "expiry"]

print("file purity (does each file contain only its own expiry?)")
for path in sorted(HF.glob("options__NIFTY__*.parquet")):
    name = path.stem.replace("options__NIFTY__", "")
    f = pd.read_parquet(path, columns=["expiry", "symbol", "trading_day"])
    exp = sorted({str(e)[:10] for e in f["expiry"].unique()})
    sym = sorted(f["symbol"].unique())
    print(f"  {name}: expiries={len(exp)}{'' if exp == [name] else ' ' + str(exp[:5])} "
          f"symbols={sym} days={f['trading_day'].nunique()}")
    del f

for iso, expiry in [("2025-09-26", "2025-09-30"), ("2025-09-25", "2025-09-30")]:
    f = pd.read_parquet(HF / f"options__NIFTY__{expiry}.parquet", columns=COLS)
    day = f[f["trading_day"].astype(str).str.slice(0, 10) == iso]
    labels = sorted({str(t)[11:16] for t in pd.to_datetime(day["timestamp"])})
    print(f"\n{iso} x {expiry}: rows={len(day):,} contracts={day.groupby(['strike','option_type']).ngroups} "
          f"labels={len(labels)} first={labels[0] if labels else '-'} last={labels[-1] if labels else '-'}")
    print(f"  last 6 labels: {labels[-6:]}   volume sum={int(day['volume'].sum()):,}")
    off = load_official(iso.replace("-", ""))
    off = off[off["expiry"] == expiry]
    off["contracts"] = pd.to_numeric(off["TtlTradgVol"], errors="coerce")
    off["lot"] = pd.to_numeric(off["NewBrdLotQty"], errors="coerce")
    print(f"  official: rows={len(off)} traded={int((off['contracts'] > 0).sum())} "
          f"contracts_sum={int(off['contracts'].sum()):,} shares_sum={int((off['contracts'] * off['lot']).sum()):,}")
    del f, day


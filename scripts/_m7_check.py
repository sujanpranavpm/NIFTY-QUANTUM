"""Sanity-check the normalisation modules against real blocks (local files only)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nifty_quant.derivatives.candles import aggregate, candle_index, candle_label
from nifty_quant.derivatives.reference import LotTable
from nifty_quant.derivatives.timestamps import classify_session
from nifty_quant.derivatives.units import normalise_units

COLS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest",
        "trading_day", "symbol", "strike", "option_type", "expiry"]
HF = Path("data/raw/p1_sample")


def block(expiry: str, day: str) -> pd.DataFrame:
    f = pd.read_parquet(HF / f"options__NIFTY__{expiry}.parquet", columns=COLS)
    return f[f["trading_day"] == day].copy()


# grid maths
assert candle_index("09:15", 5) == 0
assert candle_index("09:19", 5) == 0
assert candle_index("09:20", 5) == 1
assert candle_index("15:29", 5) == 74, candle_index("15:29", 5)
assert candle_index("15:30", 5) == 74          # closing print folded into the last bar
assert candle_label(74, 5).strftime("%H:%M") == "15:25"
assert candle_index("15:30", 15) == 24 and candle_label(24, 15).strftime("%H:%M") == "15:15"
print("grid maths ok")

# --- block-level shape survey: the evidence behind the classification thresholds ---
print(f"\n{'date':11s} {'expiry':11s} {'nlab':>5s} {'first':>6s} {'last':>6s} "
      f"{'openShare':>10s} {'termShare':>10s} {'degenL%':>8s}  verdict")
rows = []
for path in sorted(HF.glob("options__NIFTY__*.parquet")):
    expiry = path.stem.replace("options__NIFTY__", "")
    frame = pd.read_parquet(path, columns=COLS)
    days = sorted(str(d)[:10] for d in frame["trading_day"].unique())
    picks = sorted({days[0], days[-1], days[len(days) // 3], days[2 * len(days) // 3]})
    for day in picks:
        d = frame[frame["trading_day"].astype(str).str.slice(0, 10) == day]
        v = classify_session(d, trading_day=day, expiry=expiry)
        rows.append(v)
        print(f"{day:11s} {expiry:11s} {v.n_labels:5d} {v.first_label:>6s} {v.last_label:>6s} "
              f"{(v.opening_share if v.opening_share is not None else float('nan')):10.3f} "
              f"{(v.terminal_share if v.terminal_share is not None else float('nan')):10.3f} "
              f"{(v.terminal_bucket_close_pct if v.terminal_bucket_close_pct is not None else float('nan')):8.1f}"
              f"  {v.verdict}")
del frame

table = pd.DataFrame([r.__dict__ for r in rows])
print("\nverdict counts:")
print(table["verdict"].value_counts().to_string())
strict = table[table["verdict"] == "STRICT_OK"]
print(f"STRICT_OK opening share: min {strict['opening_share'].min():.3f} "
      f"median {strict['opening_share'].median():.3f} max {strict['opening_share'].max():.3f}")
print(f"STRICT_OK terminal share: min {strict['terminal_share'].min():.4f} "
      f"median {strict['terminal_share'].median():.4f} max {strict['terminal_share'].max():.4f}")

# --- targeted behaviour checks -------------------------------------------------
for expiry, day, expect in [("2025-11-25", "2025-11-25", True),
                            ("2025-01-09", "2025-01-07", False),
                            ("2024-01-25", "2024-01-18", True)]:
    d = block(expiry, day)
    v = classify_session(d, trading_day=day, expiry=expiry)
    assert v.strict_usable is expect, (day, v.verdict, v.reasons)

    res = aggregate(d, minutes=5, strict=True)
    if res.candles.empty:
        assert res.n_rejected, "strict rejection must be reported"
        print(f"{day}: rejected with reason {res.rejected['reason'].iloc[0]}")
        continue
    per = res.candles.groupby(["strike", "option_type"])["candle_open"].nunique()
    print(f"{day}: 5m rows={len(res.candles)} max candles/contract={per.max()}")
    last = res.candles.sort_values("candle_open").groupby(
        ["strike", "option_type"], observed=True).tail(1)
    print(f"      final candle is 15:25 for "
          f"{100 * (last['candle_open'].dt.strftime('%H:%M') == '15:25').mean():.1f}% of contracts; "
          f"closing print inside it for {100 * last['contains_closing_print'].mean():.1f}%")
    minute_total = float(d["volume"].sum())
    candle_total = float(res.candles["volume"].sum())
    assert abs(minute_total - candle_total) < 1e-6, (minute_total, candle_total)
    norm = normalise_units(d, LotTable())
    assert (norm["volume"] == norm["volume_share_equivalent"]).all()
    print(f"      integral share-volume rows={norm['volume_integral'].mean():.2%} "
          f"oi unit={norm['oi_unit'].unique().tolist()} "
          f"oi estimated={norm['oi_scale_estimated'].unique().tolist()}")
print("ALL CHECKS PASSED")

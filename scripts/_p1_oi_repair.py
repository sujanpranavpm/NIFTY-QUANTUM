"""P1 step 17 - can the archive's broken open interest be repaired by rescaling?

`_p1_nse_reconcile.py` showed the ratio HF_OI / official_OI sitting at 1.00 up to
the 2024-12-26 era and at 0.01 from the 2025-01-20 era on.  A ratio alone does not
answer the only question that matters for research: after multiplying by 100, does
the archive's OI actually equal the exchange's OI, for how many contracts is it
non-zero at all, and is the last bar of the day or the daily maximum the better
estimator?  This decides whether an OI-based strategy is feasible after 2025 or
whether OI must be taken from the daily official report instead.

Usage:
    python scripts/_p1_oi_repair.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path("scripts").resolve()))
from _p1_nse_reconcile import load_hf, load_official  # noqa: E402

OUT = Path("logs/p1_oi_repair.json")

# (bhavcopy date, HF expiry file, scale that the measured ratio suggests)
CASES = [
    ("20241219", "2024-12-26", 1),
    ("20241226", "2024-12-26", 1),
    ("20250123", "2025-01-30", 100),
    ("20250130", "2025-01-30", 100),
    ("20250320", "2025-03-27", 100),
    ("20250923", "2025-09-30", 100),
    ("20250930", "2025-09-30", 100),
    ("20260217", "2026-02-24", 100),
    ("20260224", "2026-02-24", 100),
]


def one(day: str, expiry: str, scale: int) -> dict:
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    official = load_official(day)
    official = official[(official["expiry"] == expiry) & (official["TtlTradgVol"] > 0)]
    hf = load_hf(expiry)
    hf_day = hf[hf["trading_day"] == iso]
    if hf_day.empty or official.empty:
        return {"date": iso, "expiry": expiry, "skipped": "no rows"}

    agg = hf_day.groupby(["strike", "option_type"], observed=True, sort=False).agg(
        last=("open_interest", "last"), peak=("open_interest", "max")).reset_index()
    merged = official.merge(agg, on=["strike", "option_type"], how="inner")
    merged["official_oi"] = merged["OpnIntrst"].astype(float)

    out: dict = {"date": iso, "expiry": expiry, "scale": scale,
                 "traded_contracts": int(len(merged))}
    for kind in ("last", "peak"):
        sub = merged[merged[kind] > 0].copy()
        out[f"nonzero_{kind}_pct"] = round(100 * len(sub) / len(merged), 2)
        if not len(sub):
            out[f"{kind}_scaled_exact_pct"] = 0.0
            continue
        guess = (sub[kind] * scale).round(0)
        err = (guess - sub["official_oi"]).abs()
        rel = err / sub["official_oi"].clip(lower=1)
        out[f"{kind}_scaled_exact_pct"] = round(100 * float((err < 0.5).mean()), 2)
        out[f"{kind}_within_1pct"] = round(100 * float((rel <= 0.01).mean()), 2)
        out[f"{kind}_within_5pct"] = round(100 * float((rel <= 0.05).mean()), 2)
        out[f"{kind}_median_abs_error"] = float(err.median())
        out[f"{kind}_ratio_official_over_hf"] = {
            "median": round(float((sub["official_oi"] / sub[kind]).median()), 4),
            "p10": round(float((sub["official_oi"] / sub[kind]).quantile(0.10)), 4),
            "p90": round(float((sub["official_oi"] / sub[kind]).quantile(0.90)), 4),
        }
    return out


def main() -> int:
    rows = [one(d, e, s) for d, e, s in CASES]
    frame = pd.DataFrame(rows)
    cols = [c for c in frame.columns
            if c not in ("expiry",) and not c.endswith("_median_abs_error")]
    print(frame[cols].to_string(index=False))
    OUT.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

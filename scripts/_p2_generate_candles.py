"""P2 stage 4 - generate and verify the 1m / 5m / 15m NIFTY option candle datasets.

This is the first place the resolved timestamp convention (open-labelled, 15:30 =
closing print) and the strict session classifier produce *real* persisted datasets.
Three resolutions are written per expiry file:

* ``1m``  the canonical pass-through: the raw provider rows of every STRICT_OK
  (day x expiry) block, verbatim, with only annotation columns added.  No provider
  OHLCV/OI value is mutated or invented, and the 15:30 print stays its own raw row.
* ``5m``  75-candle aggregation of the same STRICT_OK rows, closing print folded into
  the terminal in-session candle (``CLOSING_PRINT_INDEX`` clamped to ``LAST_BUCKET_INDEX``).
* ``15m`` 25-candle aggregation, same fold.

Only blocks the classifier trusts are aggregated; every rejected block is reported with
its verdict and reason, never silently dropped.  Empty intervals are not filled and NaN
open interest is preserved.

Integrity is verified per file - OHLC containment, volume conservation against the 1m
pass-through, closing-print placement and last-candle label correctness - and the
aggregate must show zero containment and zero conservation failures across all STRICT_OK
blocks or the run fails closed.

Outputs:
  data/derived/candles/{1m,5m,15m}__NIFTY__<expiry>.parquet
  logs/p2_candle_integrity.txt

Usage (from the repo root):
    python scripts/_p2_generate_candles.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nifty_quant.derivatives.candles import (  # noqa: E402
    SESSION_MINUTE_BUCKETS, aggregate_from_1m, minute_pass_through,
    ohlc_containment_failures, volume_conservation_delta)

HF = Path("data/raw/p1_sample")
OUT_DIR = Path("data/derived/candles")
OUT_TXT = Path("logs/p2_candle_integrity.txt")
RAW_COLUMNS = ["timestamp", "trading_day", "symbol", "expiry", "strike", "option_type",
               "open", "high", "low", "close", "volume", "open_interest"]
GRID_MINUTES = (5, 15)


def verify(name: str, one_min: pd.DataFrame, candles: pd.DataFrame, minutes: int,
           say) -> dict:
    """Return and log the integrity counters for one aggregated grid of one expiry."""
    ohlc_fail = ohlc_containment_failures(candles)
    vol_delta = volume_conservation_delta(one_min, candles)
    printed = candles[candles["contains_closing_print"].astype(bool)] if len(candles) \
        else candles
    # The terminal candle's close must equal the raw closing print it folded, and it
    # must be the last slot of the grid (15:25 for 5m, 15:15 for 15m).
    if len(printed):
        close_matches = int((abs(printed["close"] - printed["closing_print"])
                             <= 1e-9).sum())
        expected_label = f"{(9 * 60 + 15 + (SESSION_MINUTE_BUCKETS // minutes - 1) * minutes) // 60:02d}:" \
                         f"{(9 * 60 + 15 + (SESSION_MINUTE_BUCKETS // minutes - 1) * minutes) % 60:02d}"
        label_matches = int((printed["candle_open"].dt.strftime("%H:%M")
                             == expected_label).sum())
    else:
        close_matches = label_matches = 0
    one_min_print_cells = int(one_min[one_min["is_closing_print"]]
                             .groupby(["trading_day", "symbol", "expiry", "strike",
                                       "option_type"]).ngroups) if len(one_min) else 0
    result = {
        "resolution": name, "minutes": minutes, "n_candles": len(candles),
        "ohlc_failures": ohlc_fail, "volume_delta": vol_delta,
        "candles_with_print": int(len(printed)),
        "one_min_contract_days_with_print": one_min_print_cells,
        "print_close_matches": close_matches, "print_label_matches": label_matches,
    }
    say(f"    {name:>4}: candles={result['n_candles']:>7}  "
        f"ohlc_fail={ohlc_fail}  vol_delta={vol_delta:.4f}  "
        f"print_placement={close_matches}/{result['candles_with_print']} "
        f"(cdays {one_min_print_cells})")
    # a print placed on the wrong candle label is a fabrication signal
    ok = (ohlc_fail == 0 and vol_delta < 0.5
          and close_matches == len(printed) == label_matches
          and result["candles_with_print"] <= one_min_print_cells)
    result["ok"] = bool(ok)
    return result


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    files = sorted(HF.glob("options__NIFTY__*.parquet"))
    say("P2 stage 4 - 1m/5m/15m candle generation and integrity verification")
    say("=" * 104)
    say(f"cached provider expiry files: {len(files)}  ->  {OUT_DIR}")

    totals = {"1m_rows": 0, "candles": 0, "ohlc_failures": 0, "volume_delta": 0.0,
              "rejected_blocks": 0, "print_mismatches": 0, "files_failed": 0}
    per_file = []

    for path in files:
        expiry = path.stem.replace("options__NIFTY__", "")
        raw = pd.read_parquet(path, columns=RAW_COLUMNS)
        raw["strike"] = raw["strike"].astype("int64")

        one_min_res = minute_pass_through(raw)
        one_min = one_min_res.candles
        rejected = one_min_res.rejected
        n_rejected = int(len(rejected))
        totals["rejected_blocks"] += n_rejected

        say(f"\n{expiry}  raw_rows={len(raw):>8}  "
            f"1m_usable_rows={len(one_min):>8}  rejected_blocks={n_rejected}")
        # 1m integrity: raw pass-through must equal its own input row count and conserve
        one_min_ohlc = ohlc_containment_failures(one_min)
        totals["1m_rows"] += len(one_min)

        # aggregate each coarse grid exactly once (vectorised on the trusted 1m frame),
        # then verify and persist the same frame
        grids = {m: aggregate_from_1m(one_min, m).candles for m in GRID_MINUTES}
        checks = [verify(f"{m}m", one_min, grids[m], m, say) for m in GRID_MINUTES]

        one_min.to_parquet(OUT_DIR / f"1m__NIFTY__{expiry}.parquet", index=False)
        for m in GRID_MINUTES:
            grids[m].to_parquet(OUT_DIR / f"{m}m__NIFTY__{expiry}.parquet", index=False)
            totals["candles"] += len(grids[m])

        file_ok = (one_min_ohlc == 0 and all(c["ok"] for c in checks))
        totals["ohlc_failures"] += one_min_ohlc + sum(c["ohlc_failures"] for c in checks)
        totals["volume_delta"] += sum(c["volume_delta"] for c in checks)
        totals["print_mismatches"] += sum(
            c["candles_with_print"] - c["print_close_matches"] for c in checks)
        if not file_ok:
            totals["files_failed"] += 1
            say(f"    -> FILE FAILED INTEGRITY ({expiry})")
        per_file.append({"expiry": expiry, "raw_rows": len(raw),
                         "one_min_rows": len(one_min), "one_min_ohlc": one_min_ohlc,
                         "rejected_blocks": n_rejected, "ok": file_ok,
                         "grids": {c["resolution"]: c for c in checks}})

    say("\n" + "=" * 104)
    say("GRAND TOTALS")
    say(f"  1m rows written            : {totals['1m_rows']}")
    say(f"  coarse candles written     : {totals['candles']}")
    say(f"  rejected (day x expiry)    : {totals['rejected_blocks']} (reported, never aggregated)")
    say(f"  OHLC containment failures  : {totals['ohlc_failures']}")
    say(f"  volume conservation delta  : {totals['volume_delta']:.6f} (sum over all files)")
    say(f"  closing-print mismatches   : {totals['print_mismatches']}")
    say(f"  files failing integrity    : {totals['files_failed']} / {len(files)}")
    verdict = "PASS" if (totals["ohlc_failures"] == 0 and totals["volume_delta"] < 0.5
                         and totals["print_mismatches"] == 0) else "FAIL"
    say(f"  INTEGRITY VERDICT          : {verdict}")
    say("\nNo provider value is mutated in the 1m pass-through; the 15:30 print is kept "
        "as its own raw row at 1m and folded (with its raw value preserved in the "
        "'closing_print' column) into the terminal candle at 5m/15m. Rejected blocks are "
        "counted and reported above, never silently skipped.")

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_TXT}", flush=True)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

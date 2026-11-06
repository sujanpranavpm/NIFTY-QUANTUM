"""P2.2.4 stage 6 - regenerate 1m/5m/15m NIFTY option candles for the EXPANDED cache.

This runs the SAME validated pipeline as ``scripts/_p2_generate_candles.py`` (the exact
functions ``minute_pass_through`` / ``aggregate_from_1m`` plus that script's ``verify``
integrity checks and identical output paths and grand-total aggregation).  Each expiry
file is independent, so the per-file body is farmed out to a process pool purely for
throughput - the numbers produced are identical to a sequential run of the validated
script, and no aggregation logic is reimplemented here.

    $env:PYTHONPATH='.;scripts'; python scripts/_p224_candles.py

Outputs: data/derived/candles/{1m,5m,15m}__NIFTY__<expiry>.parquet
         logs/p224_candle_integrity.txt
         logs/p224_candle_integrity.json
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _p2_generate_candles import (  # noqa: E402  (reuse the validated pipeline verbatim)
    GRID_MINUTES, HF, OUT_DIR, RAW_COLUMNS, verify)
from nifty_quant.derivatives.candles import (  # noqa: E402
    aggregate_from_1m, minute_pass_through, ohlc_containment_failures)

OUT_TXT = Path("logs/p224_candle_integrity.txt")
OUT_JSON = Path("logs/p224_candle_integrity.json")


def process(path: Path) -> dict:
    """One expiry file -> write {1m,5m,15m} and return its integrity tallies."""
    expiry = path.stem.replace("options__NIFTY__", "")
    raw = pd.read_parquet(path, columns=RAW_COLUMNS)
    raw["strike"] = raw["strike"].astype("int64")

    one_min_res = minute_pass_through(raw)
    one_min = one_min_res.candles
    n_rejected = int(len(one_min_res.rejected))
    one_min_ohlc = ohlc_containment_failures(one_min)

    grids = {m: aggregate_from_1m(one_min, m).candles for m in GRID_MINUTES}
    checks = [verify(f"{m}m", one_min, grids[m], m, lambda *_, **__: None)
              for m in GRID_MINUTES]

    one_min.to_parquet(OUT_DIR / f"1m__NIFTY__{expiry}.parquet", index=False)
    for m in GRID_MINUTES:
        grids[m].to_parquet(OUT_DIR / f"{m}m__NIFTY__{expiry}.parquet", index=False)

    file_ok = one_min_ohlc == 0 and all(c["ok"] for c in checks)
    return {
        "expiry": expiry, "raw_rows": int(len(raw)), "one_min_rows": int(len(one_min)),
        "one_min_ohlc": int(one_min_ohlc), "rejected_blocks": n_rejected, "ok": bool(file_ok),
        "candles": int(sum(len(grids[m]) for m in GRID_MINUTES)),
        "ohlc_failures": int(one_min_ohlc + sum(c["ohlc_failures"] for c in checks)),
        "volume_delta": float(sum(c["volume_delta"] for c in checks)),
        "print_mismatches": int(sum(c["candles_with_print"] - c["print_close_matches"]
                                    for c in checks)),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(HF.glob("options__NIFTY__*.parquet"))
    workers = max(1, min(5, len(files)))
    print(f"P2.2.4 candle regeneration: {len(files)} files across {workers} workers",
          flush=True)

    with mp.Pool(workers) as pool:
        results = pool.map(process, files, chunksize=1)

    totals = {"1m_rows": 0, "candles": 0, "ohlc_failures": 0, "volume_delta": 0.0,
              "rejected_blocks": 0, "print_mismatches": 0, "files_failed": 0}
    for r in results:
        totals["1m_rows"] += r["one_min_rows"]
        totals["candles"] += r["candles"]
        totals["rejected_blocks"] += r["rejected_blocks"]
        totals["ohlc_failures"] += r["ohlc_failures"]
        totals["volume_delta"] += r["volume_delta"]
        totals["print_mismatches"] += r["print_mismatches"]
        if not r["ok"]:
            totals["files_failed"] += 1

    lines = ["P2.2.4 - 1m/5m/15m candle integrity (expanded cache, validated pipeline)",
             "=" * 72,
             f"files processed          : {len(results)}",
             f"1m rows written          : {totals['1m_rows']:,}",
             f"coarse candles written   : {totals['candles']:,}",
             f"rejected (day x expiry)  : {totals['rejected_blocks']}",
             f"OHLC containment failures: {totals['ohlc_failures']}",
             f"volume conservation delta: {totals['volume_delta']:.6f}",
             f"closing-print mismatches : {totals['print_mismatches']}",
             f"files failing integrity  : {totals['files_failed']} / {len(results)}"]
    verdict = "PASS" if (totals["ohlc_failures"] == 0 and totals["volume_delta"] < 0.5
                         and totals["print_mismatches"] == 0) else "FAIL"
    lines.append(f"INTEGRITY VERDICT        : {verdict}")
    for r in results:
        if not r["ok"]:
            lines.append(f"  FAILED {r['expiry']}: ohlc={r['ohlc_failures']} "
                         f"vol={r['volume_delta']:.4f} print_mm={r['print_mismatches']}")
    text = "\n".join(lines) + "\n"
    OUT_TXT.write_text(text, encoding="utf-8")
    OUT_JSON.write_text(json.dumps({"totals": totals, "verdict": verdict,
                                    "per_file": results}, indent=1), encoding="utf-8")
    print(text, flush=True)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only repository audit. Prints the actual implemented state.

Written to a file rather than a one-liner so the audit is repeatable and
does not depend on shell quoting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from nifty_quant.config import ResearchConfig
from nifty_quant.strategies.registry import excluded_ids, spec_table

pd.set_option("display.width", 250)
pd.set_option("display.max_colwidth", 40)

ROOT = Path(__file__).resolve().parents[1]

print("=" * 78)
print("STRATEGY REGISTRY")
print("=" * 78)
table = spec_table()
cols = [
    "strategy_id",
    "name",
    "family",
    "timeframe",
    "run_mode",
    "direction",
    "applicable_to_nse_equity",
    "adaptation_notes",
]
present = [c for c in cols if c in table.columns]
print(table[present].to_string(index=False))
print(f"\ntotal={len(table)} excluded_from_equity={excluded_ids()}")

print("=" * 78)
print("RESEARCH CONFIG")
print("=" * 78)
cfg = ResearchConfig()
print(json.dumps(cfg.as_dict(), indent=2, default=str)[:3000])

print("=" * 78)
print("ARTIFACTS ON DISK")
print("=" * 78)
for name in ["smoke", "validation_smoke", "report_smoke", "signals_smoke", "roundtrip"]:
    d = ROOT / "artifacts" / name
    if not d.exists():
        print(f"{name:20s} MISSING")
        continue
    files = sorted(p.name for p in d.rglob("*") if p.is_file())
    print(f"{name:20s} {len(files):4d} files  e.g. {files[:6]}")

meta = ROOT / "artifacts" / "smoke" / "run_metadata.json"
if meta.exists():
    payload = json.loads(meta.read_text(encoding="utf-8"))
    print("\nsmoke run metadata keys:", sorted(payload))
    for key in ("interval", "universe", "universe_size", "strategy_ids", "symbols", "n_symbols"):
        if key in payload:
            value = payload[key]
            print(f"  {key}: {str(value)[:160]}")

verdicts = ROOT / "artifacts" / "validation_smoke" / "verdicts.csv"
if verdicts.exists():
    v = pd.read_csv(verdicts)
    print("\nvalidation verdict counts:", v["verdict"].value_counts().to_dict())
    print("strategies validated:", sorted(v["strategy_id"]))

print("=" * 78)
print("DATA CACHE CONTENTS")
print("=" * 78)
cache = ROOT / "data" / "cache"
for sub in sorted(cache.rglob("*")):
    if sub.is_file():
        print(f"  {sub.relative_to(cache)}  {sub.stat().st_size:,} bytes")

print("=" * 78)
print("TESTS PRESENT")
print("=" * 78)
for t in sorted((ROOT / "tests").glob("*.py")):
    n = len([ln for ln in t.read_text(encoding="utf-8").splitlines() if ln.startswith("def test_")])
    print(f"  {t.name}: {n} test functions")
print("=" * 78)

"""Read-only probe of available tooling for the P1 data audit.

Reports which parquet/columnar libraries are importable and whether the network
paths needed for sample inspection respond. Performs no writes.
"""

from __future__ import annotations

import importlib
import json
import urllib.error
import urllib.request

MODULES = [
    "pyarrow",
    "pyarrow.parquet",
    "polars",
    "duckdb",
    "huggingface_hub",
    "pandas",
    "fastparquet",
    "requests",
    "yfinance",
]

print("=" * 70)
print("LIBRARIES")
print("=" * 70)
for name in MODULES:
    try:
        mod = importlib.import_module(name)
        version = getattr(mod, "__version__", "n/a")
        print(f"  {name:22s} present  {version}")
    except Exception as exc:
        print(f"  {name:22s} MISSING  {type(exc).__name__}: {exc}")

print("=" * 70)
print("NETWORK REACHABILITY (HEAD only, no payload downloaded)")
print("=" * 70)
TARGETS = {
    "huggingface_api_dataset": "https://huggingface.co/api/datasets/thetrademarkk/india-index-options-1m",
    "huggingface_tree": "https://huggingface.co/api/datasets/thetrademarkk/india-index-options-1m/tree/main",
    "nse_reports": "https://www.nseindia.com/all-reports-derivatives",
    "cloudtraderpro_free": "https://cloudtraderpro.in/free-historical-options-data/",
    "icici_breeze_api": "https://api.icicidirect.com/",
}
for label, url in TARGETS.items():
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (research data audit)",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read(400)
            print(f"  {label:24s} HTTP {response.status}  ct={response.headers.get('content-type')}  bytes_peek={len(body)}")
    except urllib.error.HTTPError as exc:
        print(f"  {label:24s} HTTP {exc.code} {exc.reason}")
    except Exception as exc:
        print(f"  {label:24s} FAILED  {type(exc).__name__}: {exc}")

print("=" * 70)
print("HF DATASET TREE (json, names only - no data files fetched)")
print("=" * 70)
url = "https://huggingface.co/api/datasets/thetrademarkk/india-index-options-1m/tree/main"
try:
    request = urllib.request.Request(url, headers={"User-Agent": "research-audit"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    entries = payload if isinstance(payload, list) else payload.get("entries", [])
    print(f"  entries at root: {len(entries)}")
    for entry in entries[:40]:
        print(f"    {entry.get('type'):6s} {entry.get('path')}  size={entry.get('size', entry.get('lfs', {}).get('size') if entry.get('lfs') else None)}")
except Exception as exc:
    print(f"  FAILED {type(exc).__name__}: {exc}")

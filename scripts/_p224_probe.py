"""P2.2.4 feasibility probe: one missing file end-to-end (download, size, sha256,
schema, CE/PE, date range).  Writes nothing to the cache.  Run from repo root.
"""
import io
import json
import pathlib
import urllib.request

import pandas as pd

REPO = "thetrademarkk/india-index-options-1m"
REV = "main"
UA = {"User-Agent": "nifty-quant-audit"}
import hashlib


def get(url, timeout=180):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def main():
    data = json.loads(get(
        f"https://huggingface.co/api/datasets/{REPO}/tree/{REV}/options/NIFTY").read())
    cached = {p.stem.replace("options__NIFTY__", "")
              for p in pathlib.Path("data/raw/p1_sample").glob("options__NIFTY__*.parquet")}
    missing = [e for e in data if e["path"].rsplit("/", 1)[-1][:-8] not in cached]
    print(f"published={len(data)}  cached={len(cached)}  missing={len(missing)}")

    # a missing file within the index-data span (2022) for a representative probe
    probe = next((e for e in missing if e["path"].endswith("2022-01-20.parquet")), missing[0])
    stem = probe["path"].rsplit("/", 1)[-1][:-8]
    size = int(probe.get("size") or probe.get("lfs", {}).get("size") or 0)
    lfs_sha = probe.get("lfs", {}).get("oid") or probe.get("oid")
    print("probe:", stem, "size", size, "sha256(lfs.oid)", lfs_sha[:16])

    raw = get(f"https://huggingface.co/datasets/{REPO}/resolve/{REV}/{probe['path']}").read()
    sha = hashlib.sha256(raw).hexdigest()
    print("downloaded bytes:", len(raw), "size_match:", len(raw) == size)
    print("sha256:", sha[:16], "sha_match:", sha == lfs_sha)

    df = pd.read_parquet(io.BytesIO(raw))
    print("rows:", len(df), "cols:", list(df.columns))
    print("option_types:", sorted(set(df["option_type"].unique())))
    print("expiry sample:", str(df["expiry"].iloc[0])[:10],
          "trading_day:", str(df["trading_day"].min())[:10], "..", str(df["trading_day"].max())[:10])
    print("strikes:", df["strike"].nunique(), "CE:", int((df["option_type"] == "CE").sum()),
          "PE:", int((df["option_type"] == "PE").sum()))


if __name__ == "__main__":
    main()

"""P1 step 1 - map the Hugging Face dataset structure.

Uses only the HF HTTP API. Lists paths and sizes; downloads no data files.
The dataset README is fetched because it is text metadata, not market data.

Usage:
    python scripts/_p1_hf_survey.py [subpath ...]

Without arguments it walks the top-level directories and reports their shape.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

REPO = "thetrademarkk/india-index-options-1m"
BASE = "https://huggingface.co/api/datasets/" + REPO
UA = {"User-Agent": "nifty-quant-data-audit"}


def get_json(path: str, **params) -> object:
    query = urllib.parse.urlencode(params)
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as response:
        return json.load(response)


def list_dir(path: str) -> list[dict]:
    """List one directory, following the HF cursor pagination."""
    out: list[dict] = []
    query: dict[str, object] = {"recursive": "false", "limit": 1000}
    while True:
        payload = get_json("/tree/main/" + path, **query)
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        out.extend(entries)
        cursor = None
        if isinstance(payload, dict):
            cursor = payload.get("cursor") or payload.get("nextCursor")
        if not cursor or not entries:
            break
        query["cursor"] = cursor
    return out


def human(num: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024:
            return f"{num:,.1f}{unit}"
        num /= 1024
    return f"{num:,.1f}PB"


def size_of(entry: dict) -> int:
    if entry.get("lfs"):
        return int(entry["lfs"].get("size") or 0)
    return int(entry.get("size") or 0)


def main() -> int:
    print("=" * 78)
    print("DATASET METADATA")
    print("=" * 78)
    info = get_json("")
    for key in ("id", "author", "license", "private", "gated", "downloads", "likes"):
        if key in info:
            print(f"  {key:12s} {info[key]}")
    tags = info.get("tags") or []
    print(f"  tags         {tags}")
    card = info.get("cardData") or {}
    if card:
        print(f"  cardData     {json.dumps(card, default=str)[:900]}")

    print("=" * 78)
    print("README.md (dataset card, first 3000 chars)")
    print("=" * 78)
    try:
        url = f"https://huggingface.co/datasets/{REPO}/raw/main/README.md"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as response:
            print(response.read(3000).decode("utf-8", "replace"))
    except Exception as exc:
        print(f"  README unavailable: {type(exc).__name__}: {exc}")

    targets = sys.argv[1:] or []
    if not targets:
        print("=" * 78)
        print("ROOT + SECOND LEVEL STRUCTURE")
        print("=" * 78)
        root = list_dir("")
        dirs = [e for e in root if e.get("type") == "directory"]
        files = [e for e in root if e.get("type") != "directory"]
        for entry in files:
            print(f"  file      {entry['path']:28s} {human(size_of(entry))}")
        for entry in dirs:
            children = list_dir(entry["path"])
            sub_dirs = [c for c in children if c.get("type") == "directory"]
            sub_files = [c for c in children if c.get("type") != "directory"]
            total = sum(size_of(c) for c in sub_files)
            print(f"\n  DIR {entry['path']}/")
            print(f"      subdirs={len(sub_dirs)} files={len(sub_files)} bytes={human(total)}")
            if sub_dirs:
                print(f"      first subdirs: {[d['path'].split('/')[-1] for d in sub_dirs[:15]]}")
            if sub_files:
                print(f"      sample files : {[f['path'].split('/')[-1] for f in sub_files[:8]]}")
    else:
        for target in targets:
            print("=" * 78)
            print(f"TREE: {target}")
            print("=" * 78)
            entries = list_dir(target)
            dirs = [e for e in entries if e.get("type") == "directory"]
            files = [e for e in entries if e.get("type") != "directory"]
            total = sum(size_of(f) for f in files)
            print(f"  directories={len(dirs)} files={len(files)} total={human(total)}")
            for entry in files[:200]:
                name = entry["path"].split("/")[-1]
                print(f"    {human(size_of(entry)):>10s}  {name}")
            for entry in dirs[:60]:
                name = entry["path"].split("/")[-1]
                print(f"         DIR  {name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

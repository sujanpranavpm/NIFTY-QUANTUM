"""P1 step 19 - group the inspected files by the column signature actually found.

The schema report must show what the files contain, not what the dataset card
advertises, and it must make any per-file variation explicit.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

entries = json.loads(Path("logs/p1_hf_schema.json").read_text(encoding="utf-8"))
groups: dict[tuple, list] = collections.defaultdict(list)
for entry in entries:
    name = entry.get("path") or entry.get("file") or str(entry)[:40]
    cols = entry.get("columns") or entry.get("schema") or []
    names = tuple(c.split(":")[0].strip() if isinstance(c, str) else c for c in cols)
    groups[names].append(name)

for names, files in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    print("=" * 90)
    print(f"{len(files)} file(s): {files[:4]}{' ...' if len(files) > 4 else ''}")
    for name in names:
        print(f"    - {name}")
print("\ntotal files inspected:", len(entries))

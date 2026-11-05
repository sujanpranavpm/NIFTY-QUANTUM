"""Environment reproducibility check (P2 stage 7, directive Part 9).

Fail-closed verification that the live interpreter and the installed libraries match
``requirements.lock`` - the exact stack every P2 stage was validated on.  It reads the
lock, resolves each distribution's installed version through ``importlib.metadata``, and
reports any drift (a mismatched version, or a package that is simply not installed).

The Python interpreter itself is not pip-installable, so its expected version is parsed
from the lock's comment header and checked against ``sys.version_info``.

Usage (from the repo root):
    python scripts/_p2_env_check.py

Exit status is 0 only when the interpreter and every locked distribution match exactly;
any drift exits 1 and prints the offending rows.  Writes ``logs/p2_env.txt``.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
LOG = ROOT / "logs" / "p2_env.txt"

PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.-]+)\s*$")
PY_RE = re.compile(r"CPython\s+(\d+\.\d+\.\d+)")


def parse_lock(path: Path) -> tuple[str | None, list[tuple[str, str]]]:
    """Return (expected_python_version, [(distribution, pinned_version), ...])."""
    expected_py: str | None = None
    pins: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            match = PY_RE.search(line)
            if match:
                expected_py = match.group(1)
            continue
        match = PIN_RE.match(line)
        if match:
            pins.append((match.group(1), match.group(2)))
    return expected_py, pins


def installed_version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def run() -> int:
    lines: list[str] = []
    say = lines.append
    expected_py, pins = parse_lock(LOCK)
    live_py = ".".join(map(str, sys.version_info[:3]))

    say("=" * 70)
    say("NIFTY QUANT - P2 stage 7: environment / reproducibility check")
    say("=" * 70)
    say(f"requirements.lock : {LOCK}")
    py_ok = expected_py is None or live_py == expected_py
    say(f"python            : expected {expected_py}  live {live_py}  "
        f"{'OK' if py_ok else 'DRIFT'}")
    say("")
    say(f"{'package':<14}{'pinned':>12}{'installed':>14}   status")
    say("-" * 70)

    drift = 0
    for dist, pinned in pins:
        got = installed_version(dist)
        if got is None:
            status = "MISSING"
        elif got != pinned:
            status = "DRIFT"
        else:
            status = "OK"
        if status != "OK":
            drift += 1
        say(f"{dist:<14}{pinned:>12}{(got or '-'):>14}   {status}")

    say("")
    ok = py_ok and drift == 0
    say(f"packages checked: {len(pins)}   drift: {drift}   "
        f"interpreter ok: {py_ok}")
    say(f"verdict: {'REPRODUCIBLE' if ok else 'ENVIRONMENT DRIFT - not the validated stack'}")
    say("=" * 70)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())

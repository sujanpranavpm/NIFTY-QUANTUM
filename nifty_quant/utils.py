"""Small shared utilities: logging, timing, hashing, JSON encoding."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from nifty_quant.config import PATHS

_LOG_CONFIGURED = False


def get_logger(name: str = "nifty_quant", level: int = logging.INFO) -> logging.Logger:
    """Return a module logger configured once for the whole process."""
    global _LOG_CONFIGURED
    if not _LOG_CONFIGURED:
        PATHS.logs.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
            datefmt="%H:%M:%S",
        )
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root = logging.getLogger("nifty_quant")
        root.setLevel(level)
        if not root.handlers:
            root.addHandler(stream)
        file_handler = logging.FileHandler(PATHS.logs / "research.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        root.propagate = False
        _LOG_CONFIGURED = True
    return logging.getLogger(name)


@contextmanager
def timed(label: str, logger: logging.Logger | None = None) -> Iterator[None]:
    """Log the wall-clock duration of a block."""
    log = logger or get_logger("nifty_quant.timing")
    start = time.perf_counter()
    try:
        yield
    finally:
        log.info("%s finished in %.2fs", label, time.perf_counter() - start)


def stable_hash(payload: Any) -> str:
    """Deterministic short hash of any JSON-serialisable payload."""
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def pct(value: float, digits: int = 2) -> str:
    """Format a fraction as a percentage string, tolerating NaN."""
    try:
        if value != value:  # NaN
            return "n/a"
    except TypeError:
        return "n/a"
    return f"{value * 100:.{digits}f}%"

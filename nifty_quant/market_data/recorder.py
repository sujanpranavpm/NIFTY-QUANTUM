"""Recording: append-only raw frames, partitioned Parquet artifacts, no silent clobber.

Three separate kinds of artifact, because they answer different questions:

``raw/frames-*.jsonl``
    the provider's decoded messages exactly as they arrived, append-only.  When a
    normalisation bug is suspected this is the only evidence that lets the frames be
    replayed - a normalised tick cannot be un-normalised.
``ticks/part-*.parquet``
    validated, normalised observations, one file per flush so a crash costs at most
    ``flush_every_ticks`` rows instead of the session.
``candles/<tf>/part-*.parquet``
    locally aggregated candles, explicitly ``candle_origin='locally_aggregated'`` so no
    consumer can mistake them for provider-supplied OHLC.

Write rules
-----------
* Nothing is ever overwritten.  A destination that already exists is an error, matching
  the ingestion foundation's philosophy: a second write of the same artifact means the
  caller lost track of state, and silently replacing evidence is worse than failing.
* Every file is written to a temporary sibling and published with ``os.link``, so a
  reader never sees a half-written Parquet/HTML/JSON file.  Where hard links are
  unavailable the fallback still refuses to replace an existing target.
* Each session gets its own directory, so two collectors (or a restart) cannot interleave
  rows into one file and produce an unorderable tick stream.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from nifty_quant.market_data.candle_builder import CANDLE_COLUMNS


class RecordingError(RuntimeError):
    """A recording invariant was violated (usually: the target already exists)."""


def _publish(tmp: Path, target: Path) -> Path:
    """Publish ``tmp`` at ``target`` atomically, refusing to replace anything."""
    try:
        os.link(tmp, target)
        tmp.unlink(missing_ok=True)
        return target
    except FileExistsError as exc:
        tmp.unlink(missing_ok=True)
        raise RecordingError(f"refusing to overwrite existing artifact {target}") from exc
    except (PermissionError, OSError) as exc:
        if getattr(exc, "errno", None) == errno.EEXIST:
            tmp.unlink(missing_ok=True)
            raise RecordingError(f"refusing to overwrite existing artifact {target}") from exc
        # Hard links are not available here (some Windows volumes / network shares).
        # Fall back to an exclusive create: still no clobber, still atomic for readers
        # that only ever see fully-written files.
        try:
            with open(target, "xb") as handle, open(tmp, "rb") as source:
                shutil.copyfileobj(source, handle)
        except FileExistsError as exc2:
            raise RecordingError(f"refusing to overwrite existing artifact {target}") from exc2
        finally:
            tmp.unlink(missing_ok=True)
        return target


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """Write ``path`` from bytes without ever replacing an existing file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp_{uuid.uuid4().hex}_{path.name}")
    tmp.write_bytes(data)
    return _publish(tmp, path)


def write_parquet(frame: pd.DataFrame, path: Path) -> Path:
    """Serialise a frame to Snappy Parquet (the repository's storage standard)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp_{uuid.uuid4().hex}_{path.name}")
    try:
        frame.to_parquet(tmp, engine="pyarrow", compression="snappy", index=False)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return _publish(tmp, path)


def replace_file_bytes(path: Path, data: bytes) -> Path:
    """Atomically **replace** a derived state file (status snapshots, session manifests).

    Used only for files whose content is a *current view* rather than recorded evidence;
    recorded market data always goes through the no-clobber writers above.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp_{uuid.uuid4().hex}_{path.name}")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


class RawFrameRecorder:
    """Append-only JSONL log of decoded provider frames."""

    def __init__(self, path: Path, *, flush_every: int = 1) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "x": a new session must never append to another session's raw log.
        self._handle = open(self.path, "x", encoding="utf-8")
        self._pending = 0
        self.flush_every = max(1, int(flush_every))
        self.frames = 0

    def append(self, payload: dict, *, received_at: datetime | None = None) -> None:
        record = {
            "received_at_utc": (received_at or datetime.now(timezone.utc)).isoformat(
                timespec="milliseconds"),
            "frame": payload,
        }
        self._handle.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
        self.frames += 1
        self._pending += 1
        if self._pending >= self.flush_every:
            self._handle.flush()
            self._pending = 0

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def __enter__(self) -> "RawFrameRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SessionRecorder:
    """One collection session's artifact tree."""

    def __init__(self, config, *, session_id: str | None = None,
                 started_at: datetime | None = None, flush_every_ticks: int | None = None) -> None:
        self.config = config
        self.started_at = started_at or datetime.now(timezone.utc)
        stamp = self.started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.session_id = session_id or f"{stamp}-{uuid.uuid4().hex[:6]}"
        self.session_date = self.started_at.astimezone(_IST).date().isoformat()
        self.directory = (Path(config.root) / config.provider / self.session_date
                          / self.session_id)
        if self.directory.exists():
            raise RecordingError(
                f"session directory already exists: {self.directory} (a new session must "
                f"never write into an existing one)")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.flush_every = int(flush_every_ticks or config.flush_every_ticks)
        self._tick_buffer: list[dict] = []
        self._part = 0
        self.tick_rows = 0
        self.tick_files: list[Path] = []
        self.candle_files: list[Path] = []
        self.raw = RawFrameRecorder(self.directory / "raw" / "frames.jsonl")

    # -- layout ------------------------------------------------------------
    @property
    def ticks_dir(self) -> Path:
        return self.directory / "ticks"

    def candles_dir(self, minutes: int) -> Path:
        return self.directory / "candles" / f"{minutes}m"

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    def next_tick_part(self) -> Path:
        self._part += 1
        return self.ticks_dir / f"part-{self._part:04d}.parquet"

    # -- ticks -------------------------------------------------------------
    def record_tick(self, tick) -> Path | None:
        """Buffer one normalised tick, flushing when the buffer is full."""
        self._tick_buffer.append(tick.to_record())
        if len(self._tick_buffer) >= self.flush_every:
            return self.flush_ticks()
        return None

    def record_ticks(self, ticks) -> Path | None:
        for tick in ticks:
            path = self.record_tick(tick)
            if path is not None:
                return path
        return None

    def flush_ticks(self) -> Path | None:
        if not self._tick_buffer:
            return None
        frame = pd.DataFrame(self._tick_buffer)
        self._tick_buffer = []
        path = write_parquet(frame, self.next_tick_part())
        self.tick_rows += len(frame)
        self.tick_files.append(path)
        return path

    # -- candles and manifest ---------------------------------------------
    def write_candles(self, frame: pd.DataFrame, minutes: int) -> Path | None:
        if frame is None or frame.empty:
            return None
        # list(...): a bare tuple is one (MultiIndex) key to pandas, not a column set.
        path = write_parquet(frame[list(CANDLE_COLUMNS)],
                             self.candles_dir(minutes) / "candles.parquet")
        self.candle_files.append(path)
        return path

    def write_manifest(self, payload: dict) -> Path:
        body = {
            "session_id": self.session_id,
            "session_date": self.session_date,
            "provider": self.config.provider,
            "started_at_utc": self.started_at.isoformat(timespec="seconds"),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "raw_frame_file": str(self.raw.path.relative_to(self.directory)),
            "frames_recorded": self.raw.frames,
            "tick_rows": self.tick_rows,
            "tick_files": [str(p.relative_to(self.directory)) for p in self.tick_files],
            "candle_files": [str(p.relative_to(self.directory)) for p in self.candle_files],
            "data_root": str(self.directory),
            **payload,
        }
        return atomic_write_bytes(
            self.manifest_path,
            (json.dumps(body, indent=2, default=str) + "\n").encode("utf-8"))

    def close(self) -> None:
        self.flush_ticks()
        self.raw.close()


try:  # pragma: no cover - tz database presence
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST = timezone.utc

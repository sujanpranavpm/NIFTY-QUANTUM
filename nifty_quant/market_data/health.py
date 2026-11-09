"""Feed health and data-quality monitoring.

The single rule this module enforces: **an incomplete feed may never look healthy.**
A connection that is open but silent is ``STALE``; one that is open but missing
instruments is ``DEGRADED``; a rejected credential is ``AUTH_FAILED`` and never
"reconnecting".  The state is derived from observations in :meth:`DataQualityMonitor.
snapshot` rather than set once, so it cannot drift from the evidence.

The monitor is also the only writer of the status snapshot the CLI reads, so
``python -m nifty_quant.market_data status`` reports the live process's own view rather
than a second, independent guess.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from nifty_quant.market_data.providers.base import ConnectionState

try:  # pragma: no cover - tz database presence
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST = timezone.utc


@dataclass
class _ErrorRecord:
    moment: str
    kind: str
    message: str


@dataclass
class DataQualityMonitor:
    """Accumulates connection, freshness, coverage and quality evidence."""

    subscription_count: int = 0
    stale_after_seconds: float = 30.0
    #: Fraction of subscribed instruments that must have ticked before the feed is called
    #: healthy.  A feed missing half its universe is degraded, not connected.
    min_coverage: float = 0.5
    #: Share of non-usable updates above which the feed is reported degraded.
    max_bad_quality_share: float = 0.25
    expected_keys: tuple[str, ...] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    max_errors_kept: int = 50

    def __post_init__(self) -> None:
        self._connected = False
        self._reconnecting = False
        self._auth_failed = False
        self._session_closed = False
        self._last_frame_monotonic: float | None = None
        self._last_frame_utc: str | None = None
        self._state_since = datetime.now(timezone.utc)
        self._last_tick_ms: dict[str, int] = {}
        self._quality: dict[str, int] = {}
        self._reconnects = 0
        self._frames = 0
        self._ticks = 0
        self._errors: list[_ErrorRecord] = []
        self._missing_seen: dict[str, str] = {}
        self._auth_message = ""
        self._clock = time.monotonic

    # -- connection transitions -------------------------------------------
    def _transition(self) -> None:
        self._state_since = datetime.now(timezone.utc)

    def mark_connected(self) -> None:
        self._connected = True
        self._reconnecting = False
        self._auth_failed = False
        self._session_closed = False
        self._last_frame_monotonic = self._clock()
        self._transition()

    def mark_disconnected(self, reason: str = "") -> None:
        self._connected = False
        if reason:
            self.record_error("disconnect", reason)
        self._transition()

    def mark_reconnecting(self, attempt: int = 0) -> None:
        if self._connected:
            self._reconnects += 1
        self._connected = False
        self._reconnecting = True
        self._transition()
        if attempt:
            self.record_error("reconnect", f"attempt {attempt}")

    def mark_auth_failed(self, message: str) -> None:
        self._auth_failed = True
        self._connected = False
        self._reconnecting = False
        self._auth_message = message
        self.record_error("auth", message)
        self._transition()

    def mark_session_closed(self) -> None:
        self._session_closed = True
        self._connected = False
        self._reconnecting = False
        self._transition()

    # -- observations ------------------------------------------------------
    def record_frame(self, received_at: datetime | None = None) -> None:
        self._frames += 1
        self._last_frame_monotonic = self._clock()
        self._last_frame_utc = (received_at or datetime.now(timezone.utc)).isoformat(
            timespec="milliseconds")

    def record_ticks(self, ticks) -> None:
        for tick in ticks:
            self._ticks += 1
            self._quality[tick.quality] = self._quality.get(tick.quality, 0) + 1
            stamp = tick.exchange_ts_ms or tick.local_ts_ms
            key = tick.instrument_key
            previous = self._last_tick_ms.get(key)
            if previous is None or stamp > previous:
                self._last_tick_ms[key] = stamp
            if key in self._missing_seen:
                self._missing_seen.pop(key, None)

    def record_error(self, kind: str, message: str) -> None:
        self._errors.append(_ErrorRecord(
            moment=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            kind=kind, message=str(message)[:500]))
        if len(self._errors) > self.max_errors_kept:
            del self._errors[: len(self._errors) - self.max_errors_kept]

    # -- derivation --------------------------------------------------------
    def state(self, now: datetime | None = None) -> ConnectionState:
        if self._auth_failed:
            return ConnectionState.AUTH_FAILED
        if self._session_closed:
            return ConnectionState.SESSION_CLOSED
        if self._reconnecting:
            return ConnectionState.RECONNECTING
        if not self._connected:
            return ConnectionState.DISCONNECTED
        if self._last_frame_monotonic is not None:
            silence = self._clock() - self._last_frame_monotonic
            if silence > self.stale_after_seconds:
                return ConnectionState.STALE
        if self._coverage() < self.min_coverage or self._bad_quality_share() > self.max_bad_quality_share:
            return ConnectionState.DEGRADED
        return ConnectionState.CONNECTED

    def _coverage(self) -> float:
        expected = [k for k in self.expected_keys] or list(self._last_tick_ms)
        if not expected:
            return 1.0
        seen = sum(1 for key in expected if key in self._last_tick_ms)
        return seen / len(expected)

    def _bad_quality_share(self) -> float:
        total = sum(self._quality.values())
        if not total:
            return 0.0
        bad = sum(count for quality, count in self._quality.items()
                  if quality not in {"CLEAN", "ABNORMAL_MOVE"})
        return bad / total

    def missing_instruments(self) -> list[str]:
        expected = [k for k in self.expected_keys] or list(self._last_tick_ms)
        return [key for key in expected if key not in self._last_tick_ms]

    def stale_instruments(self, now_ms: int | None = None) -> list[str]:
        now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
        limit_ms = self.stale_after_seconds * 1000.0
        return [key for key, stamp in self._last_tick_ms.items() if now_ms - stamp > limit_ms]

    def snapshot(self, now: datetime | None = None) -> dict:
        moment = now or datetime.now(timezone.utc)
        now_ms = int(moment.timestamp() * 1000)
        last_ticks = self._last_tick_ms
        newest = max(last_ticks.values()) if last_ticks else None
        oldest = min(last_ticks.values()) if last_ticks else None
        return {
            "state": self.state(moment).value,
            "state_since_utc": self._state_since.isoformat(timespec="seconds"),
            "as_of_utc": moment.isoformat(timespec="seconds"),
            "started_at_utc": self.started_at.isoformat(timespec="seconds"),
            "subscription_count": self.subscription_count,
            "instruments_seen": len(last_ticks),
            "instruments_missing": self.missing_instruments()[:20],
            "instruments_missing_count": len(self.missing_instruments()),
            "instruments_stale": self.stale_instruments(now_ms)[:20],
            "instruments_stale_count": len(self.stale_instruments(now_ms)),
            "coverage": round(self._coverage(), 4),
            "frames": self._frames,
            "ticks": self._ticks,
            "reconnects": self._reconnects,
            "last_frame_utc": self._last_frame_utc,
            "seconds_since_last_frame": (
                None if self._last_frame_monotonic is None
                else round(self._clock() - self._last_frame_monotonic, 3)),
            "last_tick_utc_ist": (_ms_to_ist(newest) if newest is not None else None),
            "oldest_tick_utc_ist": (_ms_to_ist(oldest) if oldest is not None else None),
            "quality_counts": dict(sorted(self._quality.items())),
            "bad_quality_share": round(self._bad_quality_share(), 4),
            "auth_message": self._auth_message,
            "errors": [e.__dict__ for e in self._errors[-10:]],
        }

    def write_status(self, path: Path) -> Path:
        from nifty_quant.market_data.recorder import replace_file_bytes

        return replace_file_bytes(
            Path(path), (json.dumps(self.snapshot(), indent=2) + "\n").encode("utf-8"))


def _ms_to_ist(stamp_ms: int) -> str:
    return datetime.fromtimestamp(stamp_ms / 1000.0, tz=timezone.utc).astimezone(
        _IST).isoformat(timespec="milliseconds")


def read_status(path: Path) -> dict | None:
    """Read a status snapshot written by a running (or finished) collector."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

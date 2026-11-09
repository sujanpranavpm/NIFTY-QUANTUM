"""WebSocket feed management: subscribe, heartbeat, detect death, reconnect, resubscribe.

The connection is replaced, never repaired.  When a socket times out, closes, or the
provider drops it, the manager closes it, backs off, re-authorises and resubscribes the
whole universe.  Re-authorising matters: Upstox feed URLs are single-use, so reusing one
after a drop fails in a way that looks like a dead market rather than a dead URL.

Deliberate properties
---------------------
* **Exactly one connection.**  The free plan allows two; a collector needs one, and a
  second socket would duplicate every tick and double the subscription count against the
  combined limit.
* **One subscription message per mode.**  The mode is a per-request field, so keys are
  grouped by mode; one message per key would be thousands of round trips.
* **An auth failure is terminal.**  A 401 is retried zero times; retrying it produces a
  reconnect storm against a credential problem and hides the real cause.
* **No silent gap.**  Every disconnect, reconnect attempt and decode failure is reported
  to the :class:`~nifty_quant.market_data.health.DataQualityMonitor`, which is what the
  status view reads.

The socket client is injected (``connect``) so the whole reconnect/resubscribe state
machine is testable without a network or a real server.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from nifty_quant.market_data.providers.base import (
    AuthenticationError,
    ConnectionState,
    MarketDataError,
    SubscriptionMode,
)


@dataclass
class FeedStats:
    """What one run of the feed manager actually did."""

    authorize_calls: int = 0
    connections: int = 0
    reconnects: int = 0
    subscribe_messages: int = 0
    frames: int = 0
    bytes_received: int = 0
    decode_failures: int = 0
    started_at: str = ""
    ended_at: str = ""
    last_frame_at: str = ""
    stopped_reason: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _default_connect(url: str, *, timeout: float = 15.0, recv_timeout: float = 45.0):
    """Open a WebSocket using the ``websockets`` sync client (imported lazily).

    Imported here rather than at module scope so the rest of the engine - discovery,
    validation, candle building, the whole test suite - works without the client
    installed.  Live collection genuinely needs it and says so.
    """
    try:
        from websockets.sync.client import connect  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MarketDataError(
            "live collection needs the 'websockets' package (pip install websockets); "
            "everything else in nifty_quant.market_data works without it") from exc
    return connect(url, open_timeout=timeout, close_timeout=5, max_size=None)


def subscription_messages(subscriptions, *, guid_factory: Callable[[], str] | None = None) -> list[dict]:
    """V3 subscribe requests, one per subscription mode.

    ``{"guid": ..., "method": "sub", "data": {"mode": ..., "instrumentKeys": [...]}}``
    """
    guid_factory = guid_factory or (lambda: uuid.uuid4().hex)
    grouped: dict[str, list[str]] = {}
    for sub in subscriptions:
        grouped.setdefault(sub.mode.value, []).append(sub.instrument_key)
    return [
        {"guid": guid_factory(), "method": "sub",
         "data": {"mode": mode, "instrumentKeys": keys}}
        for mode, keys in sorted(grouped.items())
    ]


def unsubscribe_message(subscriptions, *, guid_factory: Callable[[], str] | None = None) -> dict:
    guid_factory = guid_factory or (lambda: uuid.uuid4().hex)
    return {"guid": guid_factory(), "method": "unsub",
            "data": {"instrumentKeys": [s.instrument_key for s in subscriptions]}}


class WebSocketManager:
    """Owns the feed connection lifecycle for one universe."""

    def __init__(self, provider, universe, config, *, connect: Callable | None = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 random_source: random.Random | None = None,
                 monitor=None) -> None:
        self.provider = provider
        self.universe = universe
        self.config = config
        self._connect = connect or _default_connect
        self._sleep = sleeper
        self._clock = clock
        self._random = random_source or random.Random(20261007)
        self.monitor = monitor
        self.stats = FeedStats()

    # -- helpers -----------------------------------------------------------
    def _note_state(self, state: ConnectionState, message: str = "") -> None:
        if self.monitor is None:
            return
        if state is ConnectionState.CONNECTED:
            self.monitor.mark_connected()
        elif state is ConnectionState.RECONNECTING:
            self.monitor.mark_reconnecting(self.stats.reconnects)
        elif state is ConnectionState.AUTH_FAILED:
            self.monitor.mark_auth_failed(message)
        elif state is ConnectionState.DISCONNECTED:
            self.monitor.mark_disconnected(message)

    def backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped by configuration.

        The exponent is clamped before the shift: an unbounded ``2 ** attempt`` overflows
        after ~1000 failed reconnects, and an overflow in the retry path would replace a
        recoverable network problem with a crash.
        """
        base = self.config.ws_backoff_base_seconds
        ceiling = self.config.ws_backoff_max_seconds
        exponent = min(30, max(0, int(attempt) - 1))
        raw = min(ceiling, base * (2 ** exponent))
        jitter = self._random.uniform(0.0, raw * 0.25) if raw > 0 else 0.0
        return min(ceiling, raw + jitter)

    # -- main loop ---------------------------------------------------------
    def run(self, on_frame: Callable[[object, datetime], None], *,
            should_stop: Callable[[], bool] | None = None,
            max_seconds: float | None = None) -> FeedStats:
        """Collect until stopped, resubscribing across every recoverable failure."""
        self.stats.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        deadline = None if max_seconds is None else self._clock() + max_seconds
        attempt = 0
        stop_check = should_stop or (lambda: False)
        while not stop_check():
            if deadline is not None and self._clock() >= deadline:
                self.stats.stopped_reason = f"max_seconds {max_seconds} reached"
                break
            try:
                uri = self.provider.authorize_feed()
                self.stats.authorize_calls += 1
            except AuthenticationError as exc:
                self.stats.stopped_reason = "authentication failed"
                self._note_state(ConnectionState.AUTH_FAILED, str(exc))
                break
            except MarketDataError as exc:
                attempt += 1
                if self.config.ws_max_attempts and attempt >= self.config.ws_max_attempts:
                    self.stats.stopped_reason = f"authorisation failed after {attempt} attempts"
                    self._note_state(ConnectionState.DISCONNECTED, str(exc))
                    break
                self._note_state(ConnectionState.RECONNECTING, str(exc))
                self._sleep(self.backoff_seconds(attempt))
                continue

            connection = None
            try:
                connection = self._connect(
                    uri, timeout=self.config.ws_heartbeat_seconds,
                    recv_timeout=self.config.ws_recv_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - any connect failure is a reconnect
                attempt += 1
                self.stats.stopped_reason = f"connect failed: {type(exc).__name__}"
                self._note_state(ConnectionState.RECONNECTING, f"connect failed: {exc}")
                if self.config.ws_max_attempts and attempt >= self.config.ws_max_attempts:
                    break
                self._sleep(self.backoff_seconds(attempt))
                continue

            self.stats.connections += 1
            attempt = 0
            self._note_state(ConnectionState.CONNECTED)
            try:
                self._subscribe(connection)
                while not stop_check():
                    if deadline is not None and self._clock() >= deadline:
                        self.stats.stopped_reason = f"max_seconds {max_seconds} reached"
                        break
                    message = self._recv(connection)
                    if message is None:
                        self.stats.stopped_reason = "connection closed by provider"
                        break
                    self.stats.frames += 1
                    self.stats.bytes_received += len(message) if isinstance(message, (bytes, str)) else 0
                    self.stats.last_frame_at = datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds")
                    try:
                        on_frame(message, datetime.now(timezone.utc))
                    except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the feed
                        self.stats.decode_failures += 1
                        if self.monitor is not None:
                            self.monitor.record_error("frame", f"{type(exc).__name__}: {exc}")
            except _FeedClosed as exc:
                self.stats.stopped_reason = str(exc)
            finally:
                _safe_close(connection)

            if stop_check() or (deadline is not None and self._clock() >= deadline):
                break
            self.stats.reconnects += 1
            self._note_state(ConnectionState.RECONNECTING, self.stats.stopped_reason)
            if self.config.ws_max_attempts and self.stats.reconnects >= self.config.ws_max_attempts:
                self.stats.stopped_reason = (
                    f"giving up after {self.stats.reconnects} reconnect(s): "
                    f"{self.stats.stopped_reason}")
                break
            self._sleep(self.backoff_seconds(self.stats.reconnects))

        self.stats.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self.stats

    # -- internals ---------------------------------------------------------
    def _subscribe(self, connection) -> None:
        for message in subscription_messages(self.universe.subscriptions):
            payload = json.dumps(message)
            connection.send(payload)
            self.stats.subscribe_messages += 1

    def _recv(self, connection):
        """Receive one message, translating timeouts into a dead-connection signal."""
        try:
            return connection.recv(timeout=self.config.ws_recv_timeout_seconds)
        except TimeoutError as exc:
            raise _FeedClosed(
                f"no frame for {self.config.ws_recv_timeout_seconds:.0f}s; treating the "
                f"connection as dead") from exc
        except (ConnectionError, OSError) as exc:
            raise _FeedClosed(f"connection error: {exc}") from exc


class _FeedClosed(RuntimeError):
    """Internal signal: the socket is gone and must be replaced."""


def _safe_close(connection) -> None:
    closer = getattr(connection, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - closing a dead socket cannot fail the run
            pass

"""The live market-data service: discovery, collection, validation, candle rebuild.

This is the only place that wires the pieces together.  Each collaborator stays
independently testable and the service contributes exactly three things: ordering, the
error policy, and the artifacts/report a human or the CLI needs.

Error policy
------------
* A missing or expired credential is reported as ``AUTH_FAILED`` with a message telling
  the operator which variable to set - never as an empty session.
* A closed market is ``SESSION_CLOSED``, not "connected with no data".
* A single malformed frame is recorded and counted; it does not end the session.
* Every exit path writes a manifest and a final status snapshot, so an unattended run
  that was killed half-way leaves evidence of how far it got.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from nifty_quant.market_data import proto
from nifty_quant.market_data.candle_builder import (
    CandleAggregator,
    IST,
    TIMEFRAMES,
    CANDLE_COLUMNS,
    candle_quality_report,
    resample,
)
from nifty_quant.market_data.config import (
    AnalyticsToken,
    ConfigError,
    MarketDataConfig,
    MissingCredentials,
    redact,
)
from nifty_quant.market_data.health import DataQualityMonitor, read_status
from nifty_quant.market_data.instruments import discover_universe
from nifty_quant.market_data.normalizer import TickNormalizer
from nifty_quant.market_data.recorder import (
    RecordingError,
    SessionRecorder,
    replace_file_bytes,
    write_parquet,
)
from nifty_quant.market_data.session import MarketSessionManager
from nifty_quant.market_data.websocket import WebSocketManager
from nifty_quant.market_data.providers.base import (
    AuthenticationError,
    ConnectionState,
    MarketDataError,
    build_provider,
    enforce_subscription_limits,
)

EXIT_OK = 0
EXIT_CREDENTIALS = 2
EXIT_AUTH = 3
EXIT_SESSION = 4
EXIT_DATA = 5


class MarketDataService:
    """High-level operations the CLI and tests drive."""

    def __init__(self, config: MarketDataConfig | None = None, *, provider=None,
                 token: AnalyticsToken | str | None = None,
                 connect=None, sleeper=time.sleep,
                 clock=None) -> None:
        self.config = config or MarketDataConfig()
        self._provider = provider
        self._token = token
        self._connect = connect
        self._sleep = sleeper
        self._clock = clock
        self._universe = None
        self.last_report: dict = {}

    # -- collaborator construction ----------------------------------------
    @property
    def token(self) -> AnalyticsToken:
        if self._token is None:
            self._token = AnalyticsToken.from_env(var=self.config.token_env_var)
        elif isinstance(self._token, str):
            self._token = AnalyticsToken.from_value(self._token)
        return self._token

    @property
    def provider(self):
        if self._provider is None:
            self._provider = build_provider(self.config.provider, token=self.token,
                                            config=self.config)
        return self._provider

    def session_manager(self) -> MarketSessionManager:
        return MarketSessionManager(self.config,
                                    status_provider=lambda: self.provider.market_status())

    def status_path(self) -> Path:
        return self.config.root / self.config.provider / "state" / "status.json"

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        """A complete, honest view of the engine's current state."""
        token = self.token
        report: dict = {
            "provider": self.config.provider,
            "data_root": str(self.config.root),
            "timezone": self.config.timezone,
            "token_configured": token.configured,
            "token": token.masked,
            "token_expires_at": token.expires_at.isoformat() if token.expires_at else "unknown",
            "token_days_remaining": (round(token.days_remaining(), 1)
                                     if token.days_remaining() is not None else None),
            "token_expired": token.is_expired(),
            "config": self.config.as_dict(),
            "status_file": str(self.status_path()),
            "last_run": read_status(self.status_path()),
            "latest_session": str(self.latest_session() or ""),
        }
        try:
            # Without a credential the exchange cannot be asked, so the phase is derived
            # from the local session clock and reported as estimated - never as fact.
            session = MarketSessionManager(
                self.config,
                status_provider=((lambda: self.provider.market_status())
                                 if token.configured else None))
            window = session.window()
            trading, reason, estimated = session.is_trading_day()
            status = session.status()
            report["session"] = {
                **window.as_dict(),
                "now_ist": session.now().isoformat(timespec="seconds"),
                "within_session": session.is_within_session(),
                "is_trading_day": trading,
                "trading_day_reason": reason,
                "trading_day_estimated": estimated,
                "market_phase": status.phase.value,
                "market_status_raw": status.raw_status,
                "market_status_source": status.source,
                "market_status_estimated": status.estimated,
                "trading_day_reference": session.trading_days_source,
            }
        except Exception as exc:  # noqa: BLE001 - status must always answer
            report["session"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            report["provider_capabilities"] = self.provider.describe()
        except Exception as exc:  # noqa: BLE001
            report["provider_capabilities"] = {"error": f"{type(exc).__name__}: {exc}"}
        return report

    # -- discovery ---------------------------------------------------------
    def discover(self, *, as_of: date | None = None, write: bool = True):
        universe = discover_universe(self.provider, self.config, as_of=as_of)
        report = enforce_subscription_limits(universe.subscriptions, self.provider.capability)
        universe_report = universe.report()
        universe_report["limit_report"] = report
        self._universe = universe
        if write:
            path = (self.config.root / self.config.provider
                    / f"universe-{universe.generated_at[:10]}.json")
            replace_file_bytes(path, (json.dumps(universe_report, indent=2) + "\n").encode())
        return universe, universe_report

    # -- collection --------------------------------------------------------
    def collect(self, *, max_seconds: float | None = None, stop_event=None,
                publish: bool = False, force: bool = False,
                wait_for_open: bool = False, max_wait_seconds: float | None = None,
                write_candles: bool = True) -> dict:
        """Run one collection session.  Never raises for an expected condition."""
        started = datetime.now(timezone.utc)
        try:
            self.token.assert_usable(var=self.config.token_env_var)
        except MissingCredentials as exc:
            return self._failure(ConnectionState.AUTH_FAILED, str(exc), EXIT_CREDENTIALS, started)

        session = self.session_manager()
        if wait_for_open and not session.is_within_session():
            wait = session.wait_for_open(
                max_wait_seconds=max_wait_seconds,
                should_stop=None if stop_event is None else stop_event.is_set)
            if not wait["opened"]:
                return self._failure(ConnectionState.SESSION_CLOSED,
                                     f"market not open: {wait['reason']}",
                                     EXIT_SESSION, started, extra={"wait": wait})
        if not force:
            stop, reason = session.should_stop()
            if stop:
                return self._failure(ConnectionState.SESSION_CLOSED, reason, EXIT_SESSION,
                                     started)

        try:
            universe, universe_report = self.discover()
        except (MarketDataError, ValueError, ConfigError) as exc:
            code = EXIT_AUTH if isinstance(exc, AuthenticationError) else EXIT_CREDENTIALS
            return self._failure(ConnectionState.AUTH_FAILED if code == EXIT_AUTH
                                 else ConnectionState.DISCONNECTED,
                                 redact(str(exc), (self.token.value,)), code, started)

        recorder = SessionRecorder(self.config, started_at=started)
        monitor = DataQualityMonitor(
            subscription_count=len(universe.subscriptions),
            stale_after_seconds=self.config.stale_after_seconds,
            expected_keys=universe.keys(),
            started_at=started,
        )
        normalizer = TickNormalizer(
            universe,
            stale_after_seconds=self.config.stale_after_seconds,
            abnormal_move_pct=self.config.abnormal_move_pct,
            provider_name=self.config.provider,
        )
        for key, previous in universe.previous_open_interest.items():
            normalizer.set_previous_open_interest(key, previous)
        aggregator = CandleAggregator(1)
        candles: list = []
        decode_failures: list[dict] = []

        def handle_frame(payload, received_at):
            frame = self._decode_frame(payload, recorder, decode_failures, monitor)
            if frame is None:
                return
            monitor.record_frame(received_at)
            if not frame.get("feeds"):
                return  # market-status / JSON control frame
            ticks = normalizer.normalize_frame(frame, received_at=received_at)
            monitor.record_ticks(ticks)
            for tick in ticks:
                recorder.record_tick(tick)
                candles.extend(aggregator.add(tick))
            candles.extend(aggregator.flush(received_at))

        manager = WebSocketManager(
            self.provider, universe, self.config, connect=self._connect,
            sleeper=self._sleep, clock=self._clock or time.monotonic, monitor=monitor)

        def should_stop() -> bool:
            if stop_event is not None and stop_event.is_set():
                return True
            if force:
                # An explicit override stays in force for the whole run, otherwise the
                # loop would close the session on its first iteration.
                return False
            ended, _ = session.should_stop()
            return ended

        try:
            stats = manager.run(handle_frame, should_stop=should_stop,
                                max_seconds=max_seconds)
        finally:
            candles.extend(aggregator.finalize())
            recorder.close()

        one_min = pd.DataFrame([c.to_record() for c in candles], columns=CANDLE_COLUMNS)
        candle_report = {"1m": candle_quality_report(one_min)}
        written: dict[str, str] = {}
        if write_candles and not one_min.empty:
            path = recorder.write_candles(one_min, 1)
            if path is not None:
                written["1m"] = str(path)
            for minutes in TIMEFRAMES:
                if minutes == 1:
                    continue
                coarse = resample(one_min, minutes)
                candle_report[f"{minutes}m"] = candle_quality_report(coarse)
                path = recorder.write_candles(coarse, minutes)
                if path is not None:
                    written[f"{minutes}m"] = str(path)

        monitors = monitor.snapshot()
        state = ConnectionState(monitors["state"])
        if stats.stopped_reason.startswith("authentication"):
            state = ConnectionState.AUTH_FAILED
        manifest = recorder.write_manifest({
            "state": state.value,
            "provider": self.config.provider,
            "universe": universe_report,
            "health": monitors,
            "feed": stats.as_dict(),
            "ticks": recorder.tick_rows,
            "candles": candle_report,
            "candle_files_written": written,
            "decode_failures": decode_failures[:20],
            "decode_failure_count": len(decode_failures),
            "gaps": aggregator.gaps(),
            "forced_outside_session": bool(force),
        })
        monitor.write_status(self.status_path())

        report = {
            "ok": state in {ConnectionState.CONNECTED, ConnectionState.SESSION_CLOSED},
            "state": state.value,
            "session_id": recorder.session_id,
            "session_dir": str(recorder.directory),
            "manifest": str(manifest),
            "universe": universe_report,
            "feed": stats.as_dict(),
            "health": monitors,
            "ticks": recorder.tick_rows,
            "candles": candle_report,
            "candle_files": written,
            "gaps": aggregator.gaps(),
            "normalizer_counts": normalizer.counts(),
            "candle_skipped": aggregator.skipped,
            "decode_failures": decode_failures[:5],
            "started_at_utc": started.isoformat(timespec="seconds"),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        exit_code = EXIT_OK
        if state is ConnectionState.AUTH_FAILED:
            exit_code = EXIT_AUTH
        elif state in {ConnectionState.DISCONNECTED, ConnectionState.RECONNECTING}:
            exit_code = EXIT_SESSION
        report["exit_code"] = exit_code

        if publish:
            report["publish"] = self._publish(recorder.directory)
            if not report["publish"].get("ok", False):
                report["ok"] = False
                exit_code = exit_code or EXIT_DATA
                report["exit_code"] = exit_code
        self.last_report = report
        return report

    def _decode_frame(self, payload, recorder: SessionRecorder, decode_failures: list,
                      monitor: DataQualityMonitor) -> dict | None:
        if isinstance(payload, (bytes, bytearray)):
            raw = bytes(payload)
            try:
                frame = proto.decode_feed_response(raw)
            except proto.ProtobufError as exc:
                record = {"error": str(exc), "bytes": len(raw), "hex": raw[:256].hex()}
                decode_failures.append(record)
                recorder.raw.append({"decode_failed": record})
                monitor.record_error("decode", str(exc))
                return None
            recorder.raw.append(frame)
            return frame
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            recorder.raw.append({"unparsed_text": text[:2000]})
            monitor.record_error("frame", "non-JSON text frame")
            return None
        recorder.raw.append({"json": parsed})
        return {"json": parsed}

    def _publish(self, directory: Path) -> dict:
        from nifty_quant.market_data.ingestion_bridge import publish_session

        try:
            report = publish_session(directory, self.config)
            return {"ok": True, **report}
        except (PermissionError, FileNotFoundError, RecordingError, FileExistsError) as exc:
            return {"ok": False, "error": str(exc)}

    # -- post-session operations ------------------------------------------
    def latest_session(self) -> Path | None:
        root = self.config.root / self.config.provider
        if not root.is_dir():
            return None
        manifests = sorted(root.glob("*/*/manifest.json"))
        return manifests[-1].parent if manifests else None

    def resolve_session(self, directory: str | Path | None = None) -> Path:
        path = Path(directory) if directory else self.latest_session()
        if path is None:
            raise FileNotFoundError(
                f"no recorded session found under {self.config.root / self.config.provider}")
        if not path.is_dir():
            raise FileNotFoundError(f"session directory not found: {path}")
        return path

    def load_ticks(self, directory: str | Path | None = None) -> pd.DataFrame:
        session = self.resolve_session(directory)
        parts = sorted((session / "ticks").glob("*.parquet"))
        if not parts:
            raise FileNotFoundError(f"no tick partitions under {session / 'ticks'}")
        frames = [pd.read_parquet(part) for part in parts]
        return pd.concat(frames, ignore_index=True)

    def validate(self, directory: str | Path | None = None) -> dict:
        """Check a recorded session against the invariants the data must satisfy."""
        session = self.resolve_session(directory)
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str = "", fatal: bool = True) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail, "fatal": fatal})

        manifest_path = session / "manifest.json"
        manifest = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                check("manifest_readable", False, str(exc))
        check("manifest_present", manifest_path.is_file(), str(manifest_path))

        try:
            ticks = self.load_ticks(session)
        except FileNotFoundError as exc:
            check("ticks_present", False, str(exc))
            return {"session": str(session), "ok": False, "checks": checks}
        check("ticks_present", not ticks.empty, f"{len(ticks)} rows")

        required = {"instrument_key", "local_ts_ms", "quality", "source_provider"}
        missing = sorted(required - set(ticks.columns))
        check("tick_schema", not missing, f"missing columns: {missing}" if missing else "")
        if missing:
            return {"session": str(session), "ok": False, "checks": checks}

        check("instrument_key_present",
              bool(ticks["instrument_key"].notna().all()),
              f"{int(ticks['instrument_key'].isna().sum())} null keys")
        check("local_timestamps_present",
              bool(ticks["local_ts_ms"].notna().all()),
              f"{int(ticks['local_ts_ms'].isna().sum())} null local timestamps")
        check("single_provider",
              int(ticks["source_provider"].nunique(dropna=True)) <= 1,
              f"{sorted(ticks['source_provider'].dropna().unique())[:5]}")

        usable = ticks[ticks["quality"].isin(["CLEAN", "ABNORMAL_MOVE"])]
        check("usable_prices_present", bool(usable["ltp"].notna().any()),
              f"{int(usable['ltp'].notna().sum())} usable prices of {len(usable)} usable rows")

        if "exchange_ts_ms" in ticks.columns:
            back = 0
            for _, group in usable.groupby("instrument_key"):
                stamps = group["exchange_ts_ms"].dropna()
                if len(stamps) > 1:
                    back += int((stamps.diff().dropna() < 0).sum())
            check("exchange_time_monotonic", back == 0,
                  f"{back} regressions inside usable rows", fatal=False)

        if "volume_cumulative" in ticks.columns:
            resets = 0
            for _, group in usable.groupby("instrument_key"):
                stamps = group.sort_values("exchange_ts_ms")["volume_cumulative"].dropna()
                if len(stamps) > 1:
                    resets += int((stamps.diff().dropna() < 0).sum())
            check("cumulative_volume_monotonic", resets <= 1,
                  f"{resets} counter rollovers (at most one per instrument is a new day)",
                  fatal=False)

        candles_report: dict = {}
        candle_dir = session / "candles"
        for minutes in TIMEFRAMES:
            path = candle_dir / f"{minutes}m" / "candles.parquet"
            if not path.is_file():
                continue
            frame = pd.read_parquet(path)
            candles_report[f"{minutes}m"] = candle_quality_report(frame)
            if frame.empty:
                continue
            bad = ((frame["high"] + 1e-9 < frame[["open", "close"]].max(axis=1))
                   | (frame["low"] - 1e-9 > frame[["open", "close"]].min(axis=1))
                   | (frame["high"] + 1e-9 < frame["low"]))
            check(f"ohlc_containment_{minutes}m", not bool(bad.any()),
                  f"{int(bad.sum())} violations of {len(frame)}")
            check(f"origin_labelled_{minutes}m",
                  bool((frame["candle_origin"] == "locally_aggregated").all()),
                  "locally aggregated candles must not claim provider origin")
            if minutes > 1:
                one = pd.read_parquet(candle_dir / "1m" / "candles.parquet")
                left = float(one["volume"].sum(skipna=True))
                right = float(frame["volume"].sum(skipna=True))
                ok = abs(left - right) <= max(1e-6, abs(left) * 1e-9)
                check(f"volume_conservation_{minutes}m", ok,
                      f"1m total {left:.0f} vs {minutes}m total {right:.0f}")

        fatal_failed = [c for c in checks if c["fatal"] and not c["ok"]]
        return {
            "session": str(session),
            "session_id": session.name,
            "ok": not fatal_failed,
            "rows": int(len(ticks)),
            "instruments": int(ticks["instrument_key"].nunique()),
            "quality_counts": {str(k): int(v) for k, v in
                               ticks["quality"].value_counts().items()},
            "candles": candles_report,
            "manifest_state": manifest.get("state"),
            "manifest_ticks": manifest.get("tick_rows"),
            "checks": checks,
            "failures": [c["check"] for c in fatal_failed],
        }

    def rebuild_candles(self, directory: str | Path | None = None, *,
                        write: bool = False) -> dict:
        """Re-aggregate a recording from its ticks, without trusting the old candles.

        Useful as an audit: the rebuilt set should reproduce the recorded candles exactly,
        and any difference means either the builder changed or the recording was tampered
        with.  Rebuilt files are written under ``rebuilt/`` so nothing is overwritten.
        """
        session = self.resolve_session(directory)
        ticks = self.load_ticks(session)
        ordered = ticks.sort_values(["instrument_key", "local_ts_ms"])
        aggregator = CandleAggregator(1)
        candles = []
        for row in ordered.itertuples(index=False):
            candle = _tick_from_row(row)
            if candle is None:
                continue
            candles.extend(aggregator.add(candle))
        candles.extend(aggregator.finalize())
        one_min = pd.DataFrame([c.to_record() for c in candles], columns=CANDLE_COLUMNS)
        report = {"session": str(session), "1m": candle_quality_report(one_min),
                  "written": {}}
        if write and not one_min.empty:
            for minutes in TIMEFRAMES:
                frame = one_min if minutes == 1 else resample(one_min, minutes)
                path = write_parquet(frame, session / "rebuilt" / f"candles-{minutes}m.parquet")
                report["written"][f"{minutes}m"] = str(path)
        return report

    # -- internal ----------------------------------------------------------
    def _failure(self, state: ConnectionState, message: str, exit_code: int,
                 started: datetime, extra: dict | None = None) -> dict:
        report = {
            "ok": False,
            "state": state.value,
            "message": message,
            "exit_code": exit_code,
            "started_at_utc": started.isoformat(timespec="seconds"),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **(extra or {}),
        }
        self.last_report = report
        return report


class _UniverseShim:
    """Minimal universe stand-in so a recording can be replayed without discovery."""

    def __init__(self, ticks: pd.DataFrame) -> None:
        from nifty_quant.market_data.providers.base import InstrumentKind, Subscription, SubscriptionMode

        seen: dict[str, object] = {}
        for row in ticks.itertuples(index=False):
            key = getattr(row, "instrument_key", None)
            if not key or key in seen:
                continue
            kind_text = str(getattr(row, "kind", "option") or "option")
            try:
                kind = InstrumentKind(kind_text)
            except ValueError:
                kind = InstrumentKind.OPTION
            mode_text = str(getattr(row, "subscription_mode", "full") or "full")
            try:
                mode = SubscriptionMode(mode_text)
            except ValueError:
                mode = SubscriptionMode.FULL
            strike = getattr(row, "strike", None)
            option_type = str(getattr(row, "option_type", "") or "")
            if kind is InstrumentKind.OPTION and (strike is None or option_type not in {"CE", "PE"}):
                kind = InstrumentKind.FUTURE
            seen[key] = Subscription(
                instrument_key=key, kind=kind, mode=mode,
                trading_symbol=str(getattr(row, "trading_symbol", "") or ""),
                underlying=str(getattr(row, "underlying", "") or ""),
                expiry=str(getattr(row, "expiry", "") or ""),
                strike=None if strike is None else int(strike),
                option_type=option_type if option_type in {"CE", "PE"} else "")
        self.subscriptions = tuple(seen.values())

    def keys(self):
        return tuple(s.instrument_key for s in self.subscriptions)


def _tick_from_row(row):
    """Rebuild the minimum tick surface the aggregator needs from a stored row."""
    from nifty_quant.market_data.normalizer import USABLE_QUALITIES, Tick

    quality = str(getattr(row, "quality", "") or "")
    if quality not in USABLE_QUALITIES:
        return None
    strike = _optional(getattr(row, "strike", None), int)
    volume = _optional(getattr(row, "volume_cumulative", None), int)
    return Tick(
        instrument_key=str(row.instrument_key),
        kind=str(getattr(row, "kind", "") or ""),
        mode=str(getattr(row, "subscription_mode", "") or ""),
        exchange_ts_ms=_optional(getattr(row, "exchange_ts_ms", None), int),
        local_ts_ms=int(getattr(row, "local_ts_ms")),
        ltp=_optional(getattr(row, "ltp", None), float),
        volume=volume,
        open_interest=_optional(getattr(row, "open_interest", None), float),
        open_interest_previous=_optional(getattr(row, "open_interest_previous", None), float),
        trading_symbol=str(getattr(row, "trading_symbol", "") or ""),
        underlying=str(getattr(row, "underlying", "") or ""),
        expiry=str(getattr(row, "expiry", "") or ""),
        strike=strike,
        option_type=str(getattr(row, "option_type", "") or ""),
        source_provider=str(getattr(row, "source_provider", "") or ""),
        quality=quality,
    )


def _optional(value, caster):
    """Cast to ``caster`` or ``None``; a stored ``NaN`` is a missing value, not a number."""
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
        return caster(value)
    except (TypeError, ValueError):
        return None

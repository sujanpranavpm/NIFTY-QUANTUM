"""Live market data: recording, end-to-end collection, validation and the ingestion bridge.

The end-to-end test drives the real service against a fake provider and a scripted
WebSocket, so the ordering, the artifact tree, the manifest and the validator are all
exercised together - but nothing touches the network and no credential is real.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from nifty_quant.market_data import _test_support as T
from nifty_quant.market_data.candle_builder import CANDLE_COLUMNS, CandleAggregator
from nifty_quant.market_data.config import MarketDataConfig
from nifty_quant.market_data.ingestion_bridge import publish_session, session_artifacts
from nifty_quant.market_data.normalizer import CLEAN, Tick
from nifty_quant.market_data.recorder import (
    RecordingError,
    SessionRecorder,
    atomic_write_bytes,
    write_parquet,
)
from nifty_quant.market_data.service import EXIT_CREDENTIALS, MarketDataService

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2030, 1, 3)
SESSION_TIME = DAY.replace(hour=10, minute=0, tzinfo=IST)
TOKEN = "test-token-0123456789abcdefghijklmnop"

INDEX_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY = "NSE_INDEX|India VIX"


def sample_tick(when: datetime | None = None, *, key: str = "NSE_FO|25000CE",
                ltp: float = 100.0) -> Tick:
    when = when or SESSION_TIME
    return Tick(instrument_key=key, kind="option", mode="full",
                exchange_ts_ms=T.ms(when), local_ts_ms=T.ms(when), ltp=ltp, ltq=75,
                volume=1000, open_interest=5000.0, trading_symbol="SYM",
                underlying="NIFTY", expiry="2030-01-03", strike=25000, option_type="CE",
                source_provider="upstox", quality=CLEAN)


# ---------------------------------------------------------------------------
# Write safety
# ---------------------------------------------------------------------------
def test_atomic_write_refuses_to_replace_an_existing_artifact(tmp_path):
    path = tmp_path / "artifact.bin"
    atomic_write_bytes(path, b"first")
    with pytest.raises(RecordingError, match="refusing to overwrite"):
        atomic_write_bytes(path, b"second")
    assert path.read_bytes() == b"first"


def test_parquet_write_refuses_to_replace_an_existing_file(tmp_path):
    frame = pd.DataFrame([{"a": 1}])
    write_parquet(frame, tmp_path / "x.parquet")
    with pytest.raises(RecordingError, match="refusing to overwrite"):
        write_parquet(frame, tmp_path / "x.parquet")


def test_session_directories_are_unique_and_never_reused(tmp_path):
    config = MarketDataConfig(data_root=tmp_path)
    started = datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc)
    first = SessionRecorder(config, session_id="s1", started_at=started)
    with pytest.raises(RecordingError, match="already exists"):
        SessionRecorder(config, session_id="s1", started_at=started)
    first.close()
    assert first.directory == tmp_path / "upstox" / "2030-01-03" / "s1"


# ---------------------------------------------------------------------------
# Session artifacts
# ---------------------------------------------------------------------------
def test_session_recorder_writes_raw_frames_ticks_candles_and_manifest(tmp_path):
    config = MarketDataConfig(data_root=tmp_path, flush_every_ticks=2)
    recorder = SessionRecorder(config, session_id="s2",
                               started_at=datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc))
    recorder.raw.append({"type": "live_feed", "feeds": {}})
    assert recorder.record_tick(sample_tick()) is None      # buffered
    path = recorder.record_tick(sample_tick(DAY.replace(hour=10, minute=1, tzinfo=IST)))
    assert path is not None and path.is_file()               # flushed at the threshold
    assert recorder.tick_rows == 2

    aggregator = CandleAggregator(1)
    aggregator.add(sample_tick())
    candles = pd.DataFrame([c.to_record() for c in aggregator.finalize()],
                           columns=CANDLE_COLUMNS)
    candle_path = recorder.write_candles(candles, 1)
    manifest = recorder.write_manifest({"state": "CONNECTED"})
    recorder.close()

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["session_id"] == "s2"
    assert payload["tick_rows"] == 2
    assert payload["frames_recorded"] == 1
    assert payload["state"] == "CONNECTED"
    assert candle_path.is_file()
    assert recorder.raw.path.is_file()
    # the raw log is append-only JSONL with one record per frame
    lines = recorder.raw.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["frame"]["type"] == "live_feed"

    with pytest.raises(RecordingError, match="refusing to overwrite"):
        recorder.write_manifest({"state": "CONNECTED"})


def test_session_artifacts_lists_what_would_be_staged(tmp_path):
    directory = tmp_path / "sess"
    (directory / "ticks").mkdir(parents=True)
    (directory / "candles" / "1m").mkdir(parents=True)
    (directory / "manifest.json").write_text("{}")
    (directory / "ticks" / "part-0001.parquet").write_bytes(b"x")
    (directory / "candles" / "1m" / "candles.parquet").write_bytes(b"y")
    names = [path.name for path in session_artifacts(directory)]
    assert names == ["manifest.json", "part-0001.parquet", "candles.parquet"]
    # raw frames are opt-in: they are large and only needed for replay/debugging
    (directory / "raw").mkdir()
    (directory / "raw" / "frames.jsonl").write_text("")
    assert "frames.jsonl" not in [p.name for p in session_artifacts(directory)]
    assert "frames.jsonl" in [p.name for p in session_artifacts(directory, include_raw=True)]


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
class _ScriptedConnection:
    """Feeds scripted frames, then signals the collector to stop."""

    def __init__(self, messages, stop_event) -> None:
        self.messages = list(messages)
        self.stop_event = stop_event
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self, timeout: float | None = None):
        if self.messages:
            return self.messages.pop(0)
        self.stop_event.set()
        raise ConnectionError("scripted end of stream")

    def close(self) -> None:
        self.closed = True


def build_frame(keys: list[str], *, when: datetime, ltp: float, volume: int) -> bytes:
    feeds = {}
    for key in keys:
        stamp = T.ms(when)
        if key.startswith("NSE_INDEX"):
            feeds[key] = T.feed_index(T.index_full_feed(ltp + 24900.0, stamp, 0, ltp))
        else:
            feeds[key] = T.feed_full(T.market_full_feed(ltp, stamp, 75, ltp - 1.0,
                                                        vtt=volume, oi=5000.0, iv=12.0,
                                                        depth=[(75, ltp - 0.5, 75, ltp + 0.5)],
                                                        greeks=T.option_greeks()))
    return T.feed_response(feeds, current_ts=T.ms(when))


def make_service(tmp_path, messages, stop_event) -> MarketDataService:
    chain = T.option_chain_frame(expiry=T.FUTURE_EXPIRY_ISO, strikes=[25000], spot=25000.0)
    provider = T.FakeProvider(master=T.index_master_rows(), expiries=[T.FUTURE_EXPIRY_ISO],
                              chains={T.FUTURE_EXPIRY_ISO: chain})
    config = MarketDataConfig(data_root=tmp_path, strikes_above_atm=0,
                              strikes_below_atm=0, flush_every_ticks=2,
                              chain_cache_seconds=0.0, ws_max_attempts=3)
    return MarketDataService(
        config, provider=provider, token=TOKEN, sleeper=lambda _seconds: None,
        connect=lambda url, **kwargs: _ScriptedConnection(messages, stop_event))


def test_collect_without_a_token_reports_auth_failure_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_ANALYTICS_TOKEN", raising=False)
    service = MarketDataService(MarketDataConfig(data_root=tmp_path))
    report = service.collect(force=True)
    assert report["state"] == "AUTH_FAILED"
    assert report["exit_code"] == EXIT_CREDENTIALS
    assert not (tmp_path / "upstox").exists()


def test_collect_records_ticks_candles_and_a_manifest(tmp_path):
    chain_provider = T.FakeProvider(master=T.index_master_rows(),
                                    expiries=[T.FUTURE_EXPIRY_ISO],
                                    chains={T.FUTURE_EXPIRY_ISO:
                                            T.option_chain_frame(expiry=T.FUTURE_EXPIRY_ISO,
                                                                 strikes=[25000])})
    service = make_service(tmp_path, [], None)
    universe, _ = service.discover(write=False)
    keys = list(universe.keys())
    assert len(keys) == 5          # index + VIX + future + CE + PE

    import threading

    stop_event = threading.Event()
    messages = [
        build_frame(keys, when=SESSION_TIME, ltp=100.0, volume=1000),
        build_frame(keys, when=SESSION_TIME.replace(minute=1), ltp=101.0, volume=1040),
    ]
    service = make_service(tmp_path, messages, stop_event)
    report = service.collect(force=True, stop_event=stop_event)

    assert report["state"] == "CONNECTED"
    assert report["exit_code"] == 0
    assert report["ticks"] == 10
    assert report["universe"]["subscriptions"] == 5
    assert report["feed"]["connections"] == 1
    assert report["feed"]["subscribe_messages"] == 2      # one per subscription mode
    assert report["health"]["instruments_seen"] == 5

    session = report["session_dir"]
    manifest = json.loads((__import__("pathlib").Path(session) / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["state"] == "CONNECTED"
    assert manifest["tick_rows"] == 10
    assert manifest["universe"]["subscriptions"] == 5
    assert report["candle_files"]["1m"]

    validation = service.validate(session)
    assert validation["ok"], validation["failures"]
    assert validation["rows"] == 10
    assert validation["instruments"] == 5
    assert validation["quality_counts"].get("CLEAN", 0) >= 5


def test_validation_reports_a_session_without_a_manifest(tmp_path):
    session = tmp_path / "upstox" / "2030-01-03" / "no_manifest"
    (session / "ticks").mkdir(parents=True)
    frame = pd.DataFrame([sample_tick().to_record()])
    write_parquet(frame, session / "ticks" / "part-0001.parquet")
    service = MarketDataService(MarketDataConfig(data_root=tmp_path))
    report = service.validate(session)
    assert report["ok"] is False
    assert "manifest_present" in report["failures"]


def test_rebuilt_candles_reproduce_the_recorded_volume(tmp_path):
    service = make_service(tmp_path, [], None)
    universe, _ = service.discover(write=False)
    keys = list(universe.keys())
    import threading

    stop_event = threading.Event()
    messages = [build_frame(keys, when=SESSION_TIME, ltp=100.0, volume=1000),
                build_frame(keys, when=SESSION_TIME.replace(minute=1), ltp=101.0, volume=1040)]
    service = make_service(tmp_path, messages, stop_event)
    report = service.collect(force=True, stop_event=stop_event)
    session = report["session_dir"]

    recorded = pd.read_parquet(__import__("pathlib").Path(session) / "candles" / "1m"
                               / "candles.parquet")
    rebuilt = service.rebuild_candles(session)
    assert rebuilt["1m"]["candles"] == len(recorded)
    assert rebuilt["1m"]["ticks"] == int(recorded["n_ticks"].sum())


# ---------------------------------------------------------------------------
# Ingestion bridge
# ---------------------------------------------------------------------------
def test_bridge_refuses_to_stage_without_a_consent_record(tmp_path):
    config = MarketDataConfig(consent_record="")
    with pytest.raises(PermissionError, match="consent"):
        publish_session(tmp_path, config)


def test_bridge_stages_artifacts_with_provenance(tmp_path, monkeypatch):
    from nifty_quant.ingestion import registry

    monkeypatch.setattr(registry, "_repo_root", lambda: tmp_path)
    session = tmp_path / "live" / "upstox" / "2030-01-03" / "sess-a"
    (session / "ticks").mkdir(parents=True)
    write_parquet(pd.DataFrame([sample_tick().to_record()]),
                  session / "ticks" / "part-0001.parquet")
    (session / "manifest.json").write_text('{"state": "CONNECTED"}', encoding="utf-8")

    config = MarketDataConfig(consent_record="I attest private research use only.")
    report = publish_session(session, config)

    assert report["count"] == 2
    assert report["axis2_permission_status"] == "CONDITIONAL_PERSONAL_RESEARCH"
    assert report["source_id"].endswith("2030-01-03")
    staged_dir = tmp_path / "data" / "staging" / config.domain_id / report["source_id"]
    assert staged_dir.is_dir()
    names = sorted(p.name for p in staged_dir.iterdir())
    assert any(name.endswith(".provenance.json") for name in names)
    assert any(name.startswith("sess-a__") for name in names)
    assert all(len(item["sha256"]) == 64 for item in report["published"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_status_without_a_token_reports_the_missing_credential(tmp_path, monkeypatch,
                                                                  capsys):
    from nifty_quant.market_data.__main__ import main

    monkeypatch.delenv("UPSTOX_ANALYTICS_TOKEN", raising=False)
    code = main(["status", "--data-root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CREDENTIALS
    assert payload["token_configured"] is False
    assert payload["session"]["market_status_estimated"] is True


def test_cli_help_lists_the_operational_commands(capsys):
    from nifty_quant.market_data.__main__ import main

    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    for command in ("status", "instruments", "collect", "validate", "candles"):
        assert command in text

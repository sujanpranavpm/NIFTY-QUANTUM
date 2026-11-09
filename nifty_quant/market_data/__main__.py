"""Operational CLI: ``python -m nifty_quant.market_data <command>``.

Commands
--------
``status``       configuration, credential health, market phase, last run, latest session
``instruments``  discover and print the subscription universe (and its limits)
``collect``      run a live collection session
``validate``     audit a recorded session against the data invariants
``candles``      rebuild 1m/5m/15m candles from a recording and report completeness

Every command prints a human summary by default and JSON with ``--json``.  Credentials
are never printed: the token appears only as a mask.  Exit codes are meaningful so a
Task Scheduler entry can tell "market closed" (4) from "bad credential" (2/3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from nifty_quant.market_data.config import (
    MarketDataConfig,
    MissingCredentials,
    load_env_file,
    redact,
)
from nifty_quant.market_data.service import (
    EXIT_AUTH,
    EXIT_CREDENTIALS,
    EXIT_DATA,
    EXIT_OK,
    EXIT_SESSION,
    MarketDataService,
)


def _config_from_args(args) -> MarketDataConfig:
    """Build the engine config from the flags the selected command actually defines."""
    values: dict = {"provider": args.provider}
    if getattr(args, "data_root", None):
        values["data_root"] = Path(args.data_root)
    if getattr(args, "strikes_above", None) is not None:
        values["strikes_above_atm"] = args.strikes_above
    if getattr(args, "strikes_below", None) is not None:
        values["strikes_below_atm"] = args.strikes_below
    if getattr(args, "no_futures", False):
        values["include_futures"] = False
    if getattr(args, "no_vix", False):
        values["include_vix"] = False
    if getattr(args, "expiry_offsets", None):
        values["expiry_offsets"] = tuple(args.expiry_offsets)
    if getattr(args, "consent_record", None):
        values["consent_record"] = args.consent_record
    if not (getattr(args, "consent_record", None) or values.get("consent_record")):
        consent = os.environ.get("MARKET_DATA_CONSENT_RECORD", "").strip()
        if consent:
            values["consent_record"] = consent
    return MarketDataConfig.from_mapping(values)


def _service(args) -> MarketDataService:
    load_env_file()
    return MarketDataService(_config_from_args(args))


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    for key, value in payload.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for sub_key, sub_value in value.items():
                print(f"  {sub_key}: {sub_value}")
        elif isinstance(value, list):
            print(f"{key}: {len(value)} item(s)")
            for item in value[:10]:
                print(f"  - {item}")
        else:
            print(f"{key}: {value}")


def cmd_status(args) -> int:
    service = _service(args)
    report = service.status()
    if args.json:
        _emit(report, True)
        return EXIT_OK if report["token_configured"] else EXIT_CREDENTIALS
    session = report.get("session", {})
    print("NIFTY QUANT - live market data status")
    print(f"  provider            : {report['provider']}")
    print(f"  data root           : {report['data_root']}")
    print(f"  token               : {report['token']}"
          + ("" if report["token_configured"] else "  (not configured)"))
    if report.get("token_expired"):
        print("  NOTE                : the configured token is expired; generate a new one")
    print(f"  token expires       : {report['token_expires_at']}"
          + (f" ({report['token_days_remaining']} days)" if report["token_days_remaining"] is not None else ""))
    print(f"  now (IST)           : {session.get('now_ist', 'n/a')}")
    print(f"  session window      : {session.get('opens_at_ist', '?')} .. "
          f"{session.get('closes_at_ist', '?')}")
    print(f"  market phase        : {session.get('market_phase', '?')} "
          f"({session.get('market_status_raw', '?')})")
    print(f"  trading day         : {session.get('is_trading_day', '?')} - "
          f"{session.get('trading_day_reason', '')}")
    if session.get("market_status_estimated"):
        print("  NOTE                : phase derived from the local clock, not the exchange")
    last = report.get("last_run") or {}
    if last:
        print(f"  last run state      : {last.get('state', '?')} "
              f"(since {last.get('state_since_utc', '?')})")
        print(f"  subscriptions       : {last.get('subscription_count', '?')} "
              f"(seen {last.get('instruments_seen', '?')})")
        print(f"  latest tick         : {last.get('last_tick_utc_ist')}")
        print(f"  last frame          : {last.get('last_frame_utc')}")
        print(f"  missing instruments : {last.get('instruments_missing_count', 0)}")
        print(f"  gaps / stale        : {last.get('instruments_stale_count', 0)} stale")
        if last.get("errors"):
            print(f"  recent errors       : {last['errors'][-1].get('message', '')}")
    else:
        print("  last run            : none recorded")
    print(f"  latest session      : {report['latest_session'] or 'none'}")
    if not report["token_configured"]:
        print("\n  Set the Analytics Token in .env first: UPSTOX_ANALYTICS_TOKEN=...")
        return EXIT_CREDENTIALS
    return EXIT_OK


def cmd_instruments(args) -> int:
    service = _service(args)
    try:
        universe, report = service.discover()
    except MissingCredentials as exc:
        print(f"error: {exc}")
        return EXIT_CREDENTIALS
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim, redacted
        print(f"error: {redact(f'{type(exc).__name__}: {exc}', (service.token.value,))}")
        return EXIT_AUTH
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return EXIT_OK
    print(f"universe for {report['provider']} @ {report['generated_at']}")
    print(f"  subscriptions : {report['subscriptions']}  modes={report['by_mode']}")
    print(f"  expiries      : {report['expiries']}")
    print(f"  ATM strike    : {report['atm_strike']}  window={report['strikes']}")
    print(f"  index / VIX   : {report['index']} / {report['vix']}")
    print(f"  futures       : {report['futures']}")
    print(f"  option legs   : {report['options']}")
    for note in report.get("notes", []):
        print(f"  note          : {note}")
    if args.verbose:
        print(universe.to_frame().to_string(index=False))
    return EXIT_OK


def cmd_collect(args) -> int:
    service = _service(args)
    report = service.collect(
        max_seconds=args.max_seconds,
        publish=args.publish,
        force=args.force,
        wait_for_open=args.wait_for_open,
        max_wait_seconds=args.max_wait_seconds,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return int(report.get("exit_code", EXIT_OK))
    print(f"state         : {report['state']}")
    if report.get("message"):
        print(f"message       : {report['message']}")
    if report.get("session_dir"):
        print(f"session dir   : {report['session_dir']}")
        print(f"ticks         : {report.get('ticks')}")
        print(f"candles       : {report.get('candles')}")
        health = report.get("health", {})
        print(f"subscriptions : {health.get('subscription_count')} "
              f"(seen {health.get('instruments_seen')})")
        print(f"reconnects    : {health.get('reconnects')}")
        print(f"last tick     : {health.get('last_tick_utc_ist')}")
        if report.get("gaps"):
            print(f"gaps          : {len(report['gaps'])} instrument(s) with missing buckets")
        if report.get("publish"):
            publish = report["publish"]
            print(f"publish       : {'ok' if publish.get('ok') else publish.get('error')}")
    return int(report.get("exit_code", EXIT_OK))


def cmd_validate(args) -> int:
    service = _service(args)
    try:
        report = service.validate(args.session)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return EXIT_DATA
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return EXIT_OK if report["ok"] else EXIT_DATA
    print(f"session  : {report['session']}")
    print(f"verdict  : {'PASS' if report['ok'] else 'FAIL'}")
    print(f"rows     : {report.get('rows')}  instruments: {report.get('instruments')}")
    print(f"quality  : {report.get('quality_counts')}")
    for name, counts in report.get("candles", {}).items():
        print(f"candles {name}: {counts}")
    for check in report["checks"]:
        mark = "ok  " if check["ok"] else ("FAIL" if check["fatal"] else "warn")
        print(f"  [{mark}] {check['check']} {check['detail']}")
    return EXIT_OK if report["ok"] else EXIT_DATA


def cmd_candles(args) -> int:
    service = _service(args)
    try:
        report = service.rebuild_candles(args.session, write=args.write)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return EXIT_DATA
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return EXIT_OK
    print(f"session : {report['session']}")
    for name, counts in report.items():
        if isinstance(counts, dict):
            print(f"{name}: {counts}")
    if not args.write:
        print("(rebuilt in memory only; pass --write to persist under rebuilt/)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nifty_quant.market_data",
        description="NIFTY QUANT live market data (read-only, Upstox Analytics Token)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--provider", default="upstox", help="market-data provider")
    common.add_argument("--data-root", default=None, help="live data root (default data/live)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--verbose", action="store_true")
    common.add_argument("--session", default=None, help="recorded session directory")

    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", parents=[common], help="engine and session status")
    status.set_defaults(func=cmd_status)

    instruments = sub.add_parser("instruments", parents=[common],
                                 help="discover and print the subscription universe")
    instruments.add_argument("--strikes-above", type=int, default=None)
    instruments.add_argument("--strikes-below", type=int, default=None)
    instruments.add_argument("--expiry-offsets", type=int, nargs="*", default=None,
                             help="expiry offsets to subscribe (0 = nearest)")
    instruments.add_argument("--no-futures", action="store_true")
    instruments.add_argument("--no-vix", action="store_true")
    instruments.set_defaults(func=cmd_instruments)

    collect = sub.add_parser("collect", parents=[common],
                             help="run a live collection session")
    collect.add_argument("--max-seconds", type=float, default=None,
                         help="stop after this many seconds (default: until the close)")
    collect.add_argument("--publish", action="store_true",
                         help="stage the session in the ingestion foundation")
    collect.add_argument("--force", action="store_true",
                         help="collect even if the market looks closed")
    collect.add_argument("--wait-for-open", action="store_true",
                         help="wait for the session open before connecting")
    collect.add_argument("--max-wait-seconds", type=float, default=None)
    collect.add_argument("--consent-record", default=None,
                         help="written consent for personal-research staging")
    collect.add_argument("--strikes-above", type=int, default=None)
    collect.add_argument("--strikes-below", type=int, default=None)
    collect.add_argument("--expiry-offsets", type=int, nargs="*", default=None)
    collect.add_argument("--no-futures", action="store_true")
    collect.add_argument("--no-vix", action="store_true")
    collect.set_defaults(func=cmd_collect)

    validate = sub.add_parser("validate", parents=[common],
                              help="audit a recorded session")
    validate.set_defaults(func=cmd_validate)

    candles = sub.add_parser("candles", parents=[common],
                             help="rebuild candles from a recording")
    candles.add_argument("--write", action="store_true",
                         help="persist rebuilt candles under <session>/rebuilt/")
    candles.set_defaults(func=cmd_candles)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except MissingCredentials as exc:
        print(f"error: {exc}")
        return EXIT_CREDENTIALS
    except KeyboardInterrupt:
        print("\ninterrupted; partial data has been flushed by the session recorder")
        return EXIT_SESSION


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

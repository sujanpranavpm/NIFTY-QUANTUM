"""Live market data: configuration, credential handling and redaction.

The security properties are tested rather than asserted in prose: a token must never be
printable, a placeholder must be refused before a network call, and an expired token must
fail closed with an actionable message that contains no credential.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from nifty_quant.market_data.config import (
    AnalyticsToken,
    ConfigError,
    MarketDataConfig,
    MissingCredentials,
    TOKEN_ENV_VAR,
    decode_jwt_claims,
    load_env_file,
    looks_like_token,
    parse_env_text,
    redact,
    resolve_token,
    secret_mask,
)

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4OTM0NTYwMDAsInVpZCI6ImFiYyJ9.c2lnbmF0dXJlLXZhbHVl"


def make_jwt(payload: dict) -> str:
    def part(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part(payload)}.signature-part"


# ---------------------------------------------------------------------------
# 1. Authentication configuration
# ---------------------------------------------------------------------------
def test_token_from_env_is_configured_and_masked(tmp_path):
    token = AnalyticsToken.from_env({TOKEN_ENV_VAR: TOKEN})
    assert token.configured is True
    assert token.masked != TOKEN
    assert TOKEN[:4] in token.masked
    assert str(len(TOKEN)) in token.masked


def test_token_repr_never_contains_the_secret():
    token = AnalyticsToken.from_value(TOKEN)
    assert TOKEN not in repr(token)
    assert TOKEN not in str(token)
    assert TOKEN not in f"{token}"
    token.assert_usable()


def test_env_file_is_not_clobbering_an_exported_value(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{TOKEN_ENV_VAR}=from-file\nOTHER=1\n")
    environ = {TOKEN_ENV_VAR: "from-shell"}
    applied = load_env_file(env_file, environ)
    assert environ[TOKEN_ENV_VAR] == "from-shell"
    assert environ["OTHER"] == "1"
    assert TOKEN_ENV_VAR not in applied


def test_env_parser_handles_quotes_comments_and_export(tmp_path):
    parsed = parse_env_text(
        "# comment\n"
        "export A=1\n"
        'B="two words"\n'
        "C='single'\n"
        "D=value # trailing comment\n"
        "not a line\n"
    )
    assert parsed == {"A": "1", "B": "two words", "C": "single", "D": "value"}


def test_missing_env_file_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "absent.env", {}) == {}


def test_dotenv_example_is_placeholder_only():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert example.is_file(), ".env.example must exist with placeholders"
    text = example.read_text(encoding="utf-8")
    assert TOKEN_ENV_VAR in text
    for line in text.splitlines():
        if line.strip().startswith(f"{TOKEN_ENV_VAR}="):
            assert "your" in line.lower()
            value = line.split("=", 1)[1].strip()
            assert not looks_like_token(value)


def test_env_is_git_ignored():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [line.strip() for line in ignored]


# ---------------------------------------------------------------------------
# 2. Missing token
# ---------------------------------------------------------------------------
def test_missing_token_is_refused_before_any_request():
    token = AnalyticsToken.from_env({})
    assert token.configured is False
    with pytest.raises(MissingCredentials) as exc:
        token.assert_usable()
    assert TOKEN_ENV_VAR in str(exc.value)
    assert ".env" in str(exc.value)


def test_placeholder_token_is_refused():
    for placeholder in ("", "your-token-here", "<paste token>", "changeme", "abc"):
        token = AnalyticsToken.from_value(placeholder)
        if placeholder == "changeme":
            assert not looks_like_token(placeholder) or token.configured is False
        with pytest.raises(MissingCredentials):
            token.assert_usable()


def test_resolve_token_reads_the_configured_variable(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{TOKEN_ENV_VAR}={TOKEN}\n")
    token = resolve_token(MarketDataConfig(), env={}, load_env=False)
    # load_env=False means only the process environment is consulted.
    assert token.configured is False
    applied = load_env_file(env_file, {})
    assert applied[TOKEN_ENV_VAR] == TOKEN


# ---------------------------------------------------------------------------
# 3. Invalid token + redaction
# ---------------------------------------------------------------------------
def test_redact_removes_known_secret_and_bearer_patterns():
    masked = redact(f"Authorization: Bearer {TOKEN}", (TOKEN,))
    assert TOKEN not in masked
    assert "Bearer" in masked


def test_redact_masks_a_jwt_that_was_never_registered():
    leaked = "provider said: eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.abcdefghijklmnop"
    masked = redact(leaked)
    assert "eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ" not in masked


def test_secret_mask_short_and_empty_values():
    assert secret_mask("") == "<unset>"
    assert set(secret_mask("short")) == {"*"}


def test_jwt_claims_are_read_without_verification():
    token = make_jwt({"exp": 1893456000, "sub": "u"})
    claims = decode_jwt_claims(token)
    assert claims["sub"] == "u"
    assert decode_jwt_claims("not-a-jwt") is None


# ---------------------------------------------------------------------------
# 4. Token expiry
# ---------------------------------------------------------------------------
def test_expired_token_fails_closed_without_printing_the_secret():
    expired = datetime.now(timezone.utc) - timedelta(days=1)
    token = AnalyticsToken(value=TOKEN, expires_at=expired)
    assert token.is_expired() is True
    with pytest.raises(MissingCredentials) as exc:
        token.assert_usable()
    message = str(exc.value)
    assert "expired" in message.lower()
    assert TOKEN not in message


def test_token_expiry_is_detected_from_its_own_exp_claim():
    future = int((datetime.now(timezone.utc) + timedelta(days=200)).timestamp())
    token = AnalyticsToken.from_value(make_jwt({"exp": future}))
    assert token.expires_at is not None
    assert token.is_expired() is False
    assert 199 < token.days_remaining() < 201
    assert token.expires_within(365) is True
    assert token.expires_within(30) is False


def test_token_without_exp_claim_reports_unknown_expiry():
    token = AnalyticsToken.from_value(make_jwt({"sub": "u"}))
    assert token.expires_at is None
    assert token.days_remaining() is None
    assert token.is_expired() is False


# ---------------------------------------------------------------------------
# 5. Engine configuration guards
# ---------------------------------------------------------------------------
def test_config_defaults_are_conservative():
    config = MarketDataConfig()
    assert config.provider == "upstox"
    assert config.strikes_above_atm + config.strikes_below_atm <= 20
    assert config.expiry_offsets == (0,)
    assert config.stale_after_seconds > 0


def test_negative_strike_window_is_rejected():
    with pytest.raises(ConfigError):
        MarketDataConfig(strikes_above_atm=-1)
    with pytest.raises(ConfigError):
        MarketDataConfig(strikes_below_atm=-1)


def test_unknown_subscription_mode_is_rejected():
    with pytest.raises(ConfigError):
        MarketDataConfig(option_mode="everything")


def test_config_round_trips_through_a_mapping():
    config = MarketDataConfig(strikes_above_atm=3, expiry_offsets=(0, 1))
    rebuilt = MarketDataConfig.from_mapping({**config.as_dict(), "not_a_field": 1})
    assert rebuilt.strikes_above_atm == 3
    assert rebuilt.expiry_offsets == (0, 1)


def test_data_root_resolves_relative_to_the_repository():
    config = MarketDataConfig(data_root="data/live")
    assert config.root.is_absolute()
    assert config.root.name == "live"

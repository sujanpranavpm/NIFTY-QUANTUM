"""Hand recorded live data to the offline ingestion foundation.

The live engine produces validated Parquet under ``data/live/``.  The research layer does
not read that directory directly: it reads the curated layer, and reaching it means
passing the ingestion foundation's gates.  This module is the only bridge, and it does
not weaken a single one of them:

* the artifact is staged through ``nifty_quant.ingestion.registry.register_file``, which
  computes the SHA-256, copies atomically and writes a provenance side-car;
* the two-axis status is stated explicitly - Upstox needs a (free) account, so Axis 1 is
  ``PASS_WITH_LOGIN``; exchange data delivered through a broker API is personal research
  only, so Axis 2 is ``CONDITIONAL_PERSONAL_RESEARCH``, which the registry only accepts
  with a written consent record;
* promotion to ``data/curated/`` is **not** performed here.  It stays the owner's
  explicit decision, exactly as the P2.2.10 promotion gate specifies.

Staging an artifact never overwrites an existing one: each day gets its own source id and
each file is aliased with its session id, so two sessions on the same day cannot collide.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

#: Axis 1: a free account/token is required, and the project already uses one.
AXIS1 = "PASS_WITH_LOGIN"
#: Axis 2: proprietary exchange data, personal non-commercial research only.
AXIS2 = "CONDITIONAL_PERSONAL_RESEARCH"

CONSENT_HELP = (
    "Upstox-delivered exchange data is licensed for personal, non-commercial research, so "
    "the ingestion foundation requires an explicit written consent record before staging. "
    "Pass it explicitly (--consent-record \"...\") or set MARKET_DATA_CONSENT_RECORD in the "
    "local .env file. Nothing is staged, promoted or published without it.")


def session_artifacts(directory: Path, *, include_raw: bool = False) -> list[Path]:
    """The artifacts a session produced that are worth staging."""
    directory = Path(directory)
    wanted: list[Path] = []
    manifest = directory / "manifest.json"
    if manifest.is_file():
        wanted.append(manifest)
    wanted.extend(sorted((directory / "ticks").glob("*.parquet")))
    candles = directory / "candles"
    if candles.is_dir():
        wanted.extend(sorted(candles.glob("*/candles.parquet")))
    if include_raw:
        wanted.extend(sorted((directory / "raw").glob("*.jsonl")))
    return [path for path in wanted if path.is_file()]


def publish_session(directory: Path, config, *, registry=None,
                    include_raw: bool = False) -> dict:
    """Stage one session's artifacts with provenance.  Returns the staging report."""
    if registry is None:
        from nifty_quant.ingestion import registry as registry_module

        registry = registry_module

    consent = (config.consent_record or "").strip()
    if not consent:
        raise PermissionError(CONSENT_HELP)

    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"session directory not found: {directory}")
    session_id = directory.name
    session_day = directory.parent.name  # <root>/<provider>/<date>/<session>/...
    artifacts = session_artifacts(directory, include_raw=include_raw)
    if not artifacts:
        raise FileNotFoundError(f"no artifacts to publish under {directory}")

    source_id = f"{config.source_id}-{session_day}"
    domain_id = config.domain_id
    published: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="nq_stage_") as workdir:
        for artifact in artifacts:
            # register_file names the staged copy after the source file, so alias it with
            # the session id: two sessions on one day must not collide on part-0001.
            alias = Path(workdir) / f"{session_id}__{artifact.name}"
            shutil.copy2(artifact, alias)
            provenance = registry.register_file(
                alias,
                domain_id=domain_id,
                source_id=source_id,
                axis1_access_status=AXIS1,
                axis2_permission_status=AXIS2,
                consent_record=consent,
            )
            published.append({
                "artifact": artifact.name,
                "bytes": provenance.get("raw_file_size_bytes"),
                "sha256": provenance.get("source_sha256"),
                "artifact_id": provenance.get("artifact_id"),
                "staged_as": provenance.get("artifact_name"),
                "domain_id": domain_id,
                "source_id": source_id,
            })
    return {
        "published": published,
        "count": len(published),
        "domain_id": domain_id,
        "source_id": source_id,
        "axis1_access_status": AXIS1,
        "axis2_permission_status": AXIS2,
        "note": ("Staged only. Promotion to data/curated/ remains an explicit owner "
                 "decision (P2.2.10 promotion gate)."),
    }


def new_session_tag() -> str:
    """A short unique tag, for callers assembling their own directory names."""
    return uuid.uuid4().hex[:8]

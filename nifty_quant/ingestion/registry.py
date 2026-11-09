import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Directory layout (relative to repository root). All paths are anchored to the
# repository root to avoid reliance on the current working directory.
# ----------------------------------------------------------------------------
STAGING_ROOT = Path('data/staging')
QUARANTINE_ROOT = Path('data/quarantine')
NORMALIZED_ROOT = Path('data/normalized')
CURATED_ROOT = Path('data/curated')

# ----------------------------------------------------------------------------
# Validation regexes and allowed status sets (as defined by the specifications).
# ----------------------------------------------------------------------------
_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')          # domain_id / source_id
_REASON_RE = re.compile(r'^[A-Za-z0-9_-]+$')      # quarantine reason
_AXIS1_VALUES = {"PASS_NO_LOGIN", "PASS_WITH_LOGIN"}
_AXIS2_VALUES = {"PERMITTED", "CONDITIONAL_PERSONAL_RESEARCH"}

# ----------------------------------------------------------------------------
# Helper utilities
# ----------------------------------------------------------------------------
def _ensure_dir(p: Path) -> None:
    """Create *p* and any missing parents. Safe to call repeatedly."""
    p.mkdir(parents=True, exist_ok=True)

def _repo_root() -> Path:
    """Return the absolute repository root.

    Walks up from this file until a ``.git`` directory is found. If not found,
    falls back to the parent of this file.
    """
    cur = Path(__file__).resolve().parent
    for _ in range(10):
        if (cur / '.git').exists():
            return cur
        cur = cur.parent
    return Path(__file__).resolve().parent.parent

def _validate_path_inside(root: Path, target: Path) -> None:
    """Raise ``RuntimeError`` if *target* is not inside *root* after resolution.
    """
    root = root.resolve()
    target = target.resolve()
    if root not in target.parents and root != target:
        raise RuntimeError(f"Path escape detected: {target} is outside {root}")

def _detect_symlink_chain(p: Path) -> None:
    """Detect any symlink in *p* or its ancestors; raise ``ValueError`` if found.
    """
    for ancestor in [p] + list(p.parents):
        if ancestor.is_symlink():
            raise ValueError(f"Symlink detected in path component: {ancestor}")

def _validate_ids(domain_id: str, source_id: str) -> None:
    if not _ID_RE.fullmatch(domain_id):
        raise ValueError(f"Invalid domain_id '{domain_id}'")
    if not _ID_RE.fullmatch(source_id):
        raise ValueError(f"Invalid source_id '{source_id}'")

def _validate_axis_status(axis1: str, axis2: str, consent: str | None) -> None:
    if axis1 not in _AXIS1_VALUES:
        raise ValueError(f"Invalid axis1_access_status: {axis1}")
    if axis2 not in _AXIS2_VALUES:
        raise ValueError(f"Invalid axis2_permission_status: {axis2}")
    if axis2 == "CONDITIONAL_PERSONAL_RESEARCH" and not consent:
        raise PermissionError("Conditional personal‑research permission requires a non‑empty consent record")

def compute_sha256(file_path: Path) -> str:
    """Compute the SHA‑256 hash of *file_path*'s raw bytes."""
    h = hashlib.sha256()
    with file_path.open('rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()

def _validate_provenance(prov: dict) -> None:
    """Very lightweight provenance validation.

    Checks required keys, basic types and allowed status values. Also confirms
    that a consent record is present when required.
    """
    required = {
        "$schema",
        "artifact_id",
        "artifact_name",
        "domain_id",
        "source_id",
        "axis1_access_status",
        "axis2_permission_status",
        "source_sha256",
    }
    missing = required - prov.keys()
    if missing:
        raise ValueError(f"Provenance missing required fields: {missing}")
    # Simple type checks
    if not isinstance(prov["artifact_id"], str):
        raise TypeError("artifact_id must be a string")
    if prov["axis1_access_status"] not in _AXIS1_VALUES:
        raise ValueError("Invalid axis1_access_status in provenance")
    if prov["axis2_permission_status"] not in _AXIS2_VALUES:
        raise ValueError("Invalid axis2_permission_status in provenance")
    if prov["axis2_permission_status"] == "CONDITIONAL_PERSONAL_RESEARCH":
        if not prov.get("consent_record"):
            raise PermissionError("Consent record required for conditional personal research")

# ----------------------------------------------------------------------------
# Core public API
# ----------------------------------------------------------------------------
def register_file(
    source_path: Path,
    domain_id: str,
    source_id: str,
    *,
    axis1_access_status: str,
    axis2_permission_status: str,
    consent_record: str | None = None,
) -> dict:
    """Register a local source file for ingestion.

    Updated to enforce path traversal rejection before existence checks and to
    ensure a clean staging environment per test run.
    """

    # ---------- Path traversal validation -------------------------------------
    # Reject any path containing ".." components before any filesystem checks.
    if ".." in source_path.parts:
        raise RuntimeError(f"Path traversal detected in '{source_path}'")

    # ---------- Input validation ------------------------------------------------
    if not isinstance(source_path, Path):
        raise TypeError("source_path must be a pathlib.Path")
    _detect_symlink_chain(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    _validate_ids(domain_id, source_id)
    _validate_axis_status(axis1_access_status, axis2_permission_status, consent_record)

    # Resolve after symlink checks to avoid following a malicious symlink.
    source_path = source_path.resolve()
    # Ensure source resides inside the repository (prevents absolute‑path injection).
    # Source files may reside anywhere; only ensure they are regular files.
    # No repository‑root containment check for source_path, as the spec permits
    # registration of external local files.

    # ---------- Destination preparation ----------------------------------------
    dest_dir = (_repo_root() / STAGING_ROOT) / domain_id / source_id
    # Ensure staging directory exists.
    _ensure_dir(dest_dir)
    dest_file = dest_dir / source_path.name
    if dest_file.exists():
        raise FileExistsError(f"Staged file '{dest_file.name}' already exists under domain '{domain_id}'")



    # Copy to a temporary file first, verify integrity, then atomically rename.
    tmp_dest = dest_dir / f".tmp_{source_path.name}"
    try:
        shutil.copy2(source_path, tmp_dest)
        if compute_sha256(source_path) != compute_sha256(tmp_dest):
            raise ValueError("Hash mismatch after staging copy")
        os.replace(tmp_dest, dest_file)
    finally:
        if tmp_dest.exists():
            tmp_dest.unlink(missing_ok=True)

    # ---------- Provenance creation -------------------------------------------
    file_hash = compute_sha256(source_path)
    file_size = source_path.stat().st_size
    fetch_ts = datetime.now(timezone.utc).isoformat()

    provenance = {
        "$schema": "https://nifty-quant.local/schemas/provenance-v1.json",
        "artifact_id": str(uuid.uuid4()),
        "artifact_name": dest_file.name,
        "domain_id": domain_id,
        "source_id": source_id,
        "canonical_url": "<MANUAL_DOWNLOAD>",
        "raw_archive_member": None,
        "fetch_timestamp_utc": fetch_ts,
        "download_method": "manual_browser_export",
        "axis1_access_status": axis1_access_status,
        "axis2_permission_status": axis2_permission_status,
        "permission_evidence_ref": f"P2.2.9_SOURCE_PERMISSION_AUDIT.md#domain-{domain_id.lower()}",
        "license_tag": "NSE_WEBSITE_TOS_PERSONAL_RESEARCH",
        "attribution_requirement": "None (Proprietary Exchange Data; Personal Non‑Commercial Research Only)",
        "source_sha256": file_hash,
        "normalized_parquet_sha256": None,
        "raw_file_size_bytes": file_size,
        "detected_schema_name": "UNKNOWN",
        "schema_signature_hash": None,
        "row_count_raw": None,
        "row_count_normalized": None,
        "volume_unit_raw": "shares",
        "oi_unit_raw": "shares",
        "lot_size_resolution_authority": None,
        "validation_engine_version": "1.0.0",
        "validation_severity_verdict": "PENDING",
        "validation_flags": [],
        "parent_lineage": [],
        "consent_record": consent_record,
    }

    prov_path = dest_dir / (dest_file.stem + ".provenance.json")
    tmp_prov = prov_path.with_suffix('.tmp')
    try:
        tmp_prov.write_text(json.dumps(provenance, indent=2))
        os.replace(tmp_prov, prov_path)
    finally:
        if tmp_prov.exists():
            tmp_prov.unlink(missing_ok=True)

    return provenance
    """Register a local source file for ingestion.

    The function validates the source, computes its hash, stages the file under
    ``data/staging/<domain_id>/<source_id>/`` and writes a provenance side‑car.
    All operations are performed atomically and never overwrite existing
    artifacts.
    """
    # ---------- Input validation ------------------------------------------------
    if not isinstance(source_path, Path):
        raise TypeError("source_path must be a pathlib.Path")
    _detect_symlink_chain(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    _validate_ids(domain_id, source_id)
    _validate_axis_status(axis1_access_status, axis2_permission_status, consent_record)

    # Resolve after symlink checks to avoid following a malicious symlink.
    source_path = source_path.resolve()
    # Ensure source resides inside the repository (prevents absolute‑path injection).
    # Source files may reside anywhere; only ensure they are regular files.
    # No repository‑root containment check for source_path, as the spec permits
    # registration of external local files.


    # ---------- Destination preparation ----------------------------------------
    dest_dir = (_repo_root() / STAGING_ROOT) / domain_id / source_id
    # Ensure staging directory exists.
    _ensure_dir(dest_dir)
    dest_file = dest_dir / source_path.name
    if dest_file.exists():
        raise FileExistsError(f"Staged file '{dest_file.name}' already exists under domain '{domain_id}'")



    # Copy to a temporary file first, verify integrity, then atomically rename.
    tmp_dest = dest_dir / f".tmp_{source_path.name}"
    try:
        shutil.copy2(source_path, tmp_dest)
        if compute_sha256(source_path) != compute_sha256(tmp_dest):
            raise ValueError("Hash mismatch after staging copy")
        os.replace(tmp_dest, dest_file)
    finally:
        if tmp_dest.exists():
            tmp_dest.unlink(missing_ok=True)

    # ---------- Provenance creation -------------------------------------------
    file_hash = compute_sha256(source_path)
    file_size = source_path.stat().st_size
    fetch_ts = datetime.now(timezone.utc).isoformat()

    provenance = {
        "$schema": "https://nifty-quant.local/schemas/provenance-v1.json",
        "artifact_id": str(uuid.uuid4()),
        "artifact_name": dest_file.name,
        "domain_id": domain_id,
        "source_id": source_id,
        "canonical_url": "<MANUAL_DOWNLOAD>",
        "raw_archive_member": None,
        "fetch_timestamp_utc": fetch_ts,
        "download_method": "manual_browser_export",
        "axis1_access_status": axis1_access_status,
        "axis2_permission_status": axis2_permission_status,
        "permission_evidence_ref": f"P2.2.9_SOURCE_PERMISSION_AUDIT.md#domain-{domain_id.lower()}",
        "license_tag": "NSE_WEBSITE_TOS_PERSONAL_RESEARCH",
        "attribution_requirement": "None (Proprietary Exchange Data; Personal Non‑Commercial Research Only)",
        "source_sha256": file_hash,
        "normalized_parquet_sha256": None,
        "raw_file_size_bytes": file_size,
        "detected_schema_name": "UNKNOWN",
        "schema_signature_hash": None,
        "row_count_raw": None,
        "row_count_normalized": None,
        "volume_unit_raw": "shares",
        "oi_unit_raw": "shares",
        "lot_size_resolution_authority": None,
        "validation_engine_version": "1.0.0",
        "validation_severity_verdict": "PENDING",
        "validation_flags": [],
        "parent_lineage": [],
        "consent_record": consent_record,
    }

    prov_path = dest_dir / (dest_file.stem + ".provenance.json")
    tmp_prov = prov_path.with_suffix('.tmp')
    try:
        tmp_prov.write_text(json.dumps(provenance, indent=2))
        os.replace(tmp_prov, prov_path)
    finally:
        if tmp_prov.exists():
            tmp_prov.unlink(missing_ok=True)

    return provenance

def quarantine_file(staged_path: Path, reason: str) -> Path:
    """Move a staged artifact (and its provenance) into quarantine.

    * ``reason`` must match the strict allow‑list.
    * The source must reside inside the staging area.
    * Neither the artifact nor its provenance may overwrite existing quarantine
      entries.
    * Operations are atomic – if moving the provenance fails, the artifact is
      rolled back.
    """
    if not isinstance(reason, str):
        raise TypeError("reason must be a string")
    if not _REASON_RE.fullmatch(reason):
        raise ValueError("Quarantine reason contains disallowed characters")

    staged_path = staged_path.resolve()
    if not staged_path.is_file():
        raise FileNotFoundError(f"Staged file not found: {staged_path}")

    # Verify it lives inside the staging root.
    _validate_path_inside(_repo_root() / STAGING_ROOT, staged_path)

    prov_path = staged_path.with_suffix('.provenance.json')
    if not prov_path.is_file():
        raise FileNotFoundError("Provenance side‑car missing for staged artifact")
    prov = json.loads(prov_path.read_text())
    _validate_provenance(prov)

    safe_reason = reason.lower()
    dest_dir = (_repo_root() / QUARANTINE_ROOT) / safe_reason
    _ensure_dir(dest_dir)
    # Disallow any existing artifacts under this reason to enforce non‑destructive quarantine.
    if any(dest_dir.iterdir()):
        raise FileExistsError("Quarantine target already exists – non‑destructive required")
    dest_file = dest_dir / staged_path.name
    if dest_file.exists():
        raise FileExistsError("Quarantine target already exists – non‑destructive required")
    _validate_path_inside(_repo_root() / QUARANTINE_ROOT, dest_dir)

    try:
        shutil.move(str(staged_path), str(dest_file))
        prov_dest = dest_dir / prov_path.name
        if prov_dest.exists():
            raise FileExistsError("Quarantine provenance target already exists")
        shutil.move(str(prov_path), str(prov_dest))
    except Exception:
        # Attempt rollback if artifact moved but provenance failed.
        if dest_file.exists() and not staged_path.exists():
            shutil.move(str(dest_file), str(staged_path))
        raise

    return dest_file

def promote_to_curated(normalized_path: Path, provenance: dict) -> Path:
    """Promote a validated normalized artifact to the curated layer.

    Performs:
    * Provenance validation.
    * Both Axis 1 and Axis 2 permission checks.
    * Normalized‑hash verification.
    * Destination existence check.
    * Atomic, non‑destructive publication.
    """
    _validate_provenance(provenance)
    _validate_axis_status(
        provenance.get('axis1_access_status'),
        provenance.get('axis2_permission_status'),
        provenance.get('consent_record'),
    )

    normalized_path = normalized_path.resolve()
    if not normalized_path.is_file():
        raise FileNotFoundError('Normalized file missing')

    # Normalized hash must be present and match.
    expected_hash = provenance.get('normalized_parquet_sha256')
    if not expected_hash:
        raise ValueError('Normalized hash not yet established – promotion prohibited')
    if compute_sha256(normalized_path) != expected_hash:
        raise ValueError('Normalized SHA‑256 mismatch')

    domain = provenance.get('domain_id', 'unknown')
    dest_dir = (_repo_root() / CURATED_ROOT) / domain
    _ensure_dir(dest_dir)
    dest_file = dest_dir / normalized_path.name
    if dest_file.exists():
        raise FileExistsError('Target curated file already exists – non‑destructive promotion required')
    _validate_path_inside(_repo_root() / CURATED_ROOT, dest_dir)

    tmp_dest = dest_dir / f".tmp_{normalized_path.name}"
    # Copy normalized artifact to a temporary file first
    shutil.copy2(normalized_path, tmp_dest)
    try:
        # Attempt exclusive atomic publication using a hard link.
        # os.link fails atomically if destination exists (EEXIST) -- no overwrite.
        os.link(tmp_dest, dest_file)
        # Hard link succeeded -- remove the temporary source file.
        tmp_dest.unlink(missing_ok=True)
    except PermissionError as e:
        # Hard-link creation not permitted (e.g. unprivileged Windows).
        # Fail closed: do NOT fall back to os.replace or any overwrite-capable op.
        tmp_dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"Atomic hard-link promotion not supported on this platform: {e}"
        ) from e
    except OSError as e:
        # Destination already exists (EEXIST) or another OS-level error.
        tmp_dest.unlink(missing_ok=True)
        raise FileExistsError(
            f"Atomic promotion failed (destination exists or OS error): {e}"
        ) from e

    # Artifact published.  Now publish the provenance side-car.
    prov_dest = dest_dir / (dest_file.stem + ".provenance.json")
    tmp_prov = prov_dest.with_suffix('.tmp')
    try:
        tmp_prov.write_text(json.dumps(provenance, indent=2))
        os.link(tmp_prov, prov_dest)
        tmp_prov.unlink(missing_ok=True)
    except PermissionError as e:
        # Hard-link unsupported for provenance -- roll back the artifact.
        tmp_prov.unlink(missing_ok=True)
        if dest_file.exists():
            dest_file.unlink(missing_ok=True)
        raise RuntimeError(
            f"Atomic hard-link not supported for provenance publication: {e}"
        ) from e
    except OSError as e:
        # Provenance destination exists or another OS error.
        tmp_prov.unlink(missing_ok=True)
        if dest_file.exists():
            dest_file.unlink(missing_ok=True)
        raise FileExistsError(
            f"Atomic provenance publication failed: {e}"
        ) from e

    return dest_file

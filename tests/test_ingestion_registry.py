import json
import os
import shutil
from pathlib import Path
import pytest
pytestmark = pytest.mark.usefixtures("ingestion_isolation")

from nifty_quant.ingestion import registry

# Helper to create a temporary regular file with given content
def make_file(tmp_path: Path, name: str = "sample.txt", content: bytes = b"test data") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p

# ---------- Registration Tests -------------------------------------------------

def test_valid_registration(tmp_path: Path):
    src = make_file(tmp_path)
    prov = registry.register_file(
        src,
        domain_id="domain1",
        source_id="src1",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="CONDITIONAL_PERSONAL_RESEARCH",
        consent_record="User consent on 2022-01-01",
    )
    # provenance fields sanity
    assert prov["domain_id"] == "domain1"
    assert prov["source_id"] == "src1"
    assert prov["axis1_access_status"] == "PASS_NO_LOGIN"
    assert prov["axis2_permission_status"] == "CONDITIONAL_PERSONAL_RESEARCH"
    assert prov["consent_record"] == "User consent on 2022-01-01"
    # hash matches source file
    expected_hash = registry.compute_sha256(src)
    assert prov["source_sha256"] == expected_hash
    # staged file exists
    staged = Path(registry._repo_root()) / registry.STAGING_ROOT / "domain1" / "src1" / src.name
    assert staged.is_file()
    # provenance file exists and is valid JSON
    prov_path = staged.with_suffix('.provenance.json')
    assert prov_path.is_file()
    loaded = json.loads(prov_path.read_text())
    assert loaded == prov

def test_missing_source_raises(tmp_path: Path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(FileNotFoundError):
        registry.register_file(
            missing,
            domain_id="d",
            source_id="s",
            axis1_access_status="PASS_NO_LOGIN",
            axis2_permission_status="PERMITTED",
        )

def test_invalid_domain_id(tmp_path: Path):
    src = make_file(tmp_path)
    with pytest.raises(ValueError):
        registry.register_file(
            src,
            domain_id="invalid!id",
            source_id="src",
            axis1_access_status="PASS_NO_LOGIN",
            axis2_permission_status="PERMITTED",
        )

def test_duplicate_registration(tmp_path: Path):
    src = make_file(tmp_path)
    registry.register_file(
        src,
        domain_id="dup",
        source_id="dup",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    # second registration with same name should raise
    with pytest.raises(FileExistsError):
        registry.register_file(
            src,
            domain_id="dup",
            source_id="dup",
            axis1_access_status="PASS_NO_LOGIN",
            axis2_permission_status="PERMITTED",
        )

def test_path_traversal_rejection(tmp_path: Path):
    # create a file outside repo and try to register via ".." traversal
    external = tmp_path / "external.txt"
    external.write_text("data")
    traversal_path = Path("..") / external.name
    # The path will be resolved relative to cwd; ensure it points outside repo
    with pytest.raises(RuntimeError):
        registry.register_file(
            traversal_path,
            domain_id="d",
            source_id="s",
            axis1_access_status="PASS_NO_LOGIN",
            axis2_permission_status="PERMITTED",
        )

def test_symlink_source_detection(tmp_path: Path):
    # Attempt symlink creation; skip if not permitted (common on Windows).
    target = make_file(tmp_path, name="real.txt")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"Symlink creation not supported: {e}")
    with pytest.raises(ValueError):
        registry.register_file(
            link,
            domain_id="d",
            source_id="s",
            axis1_access_status="PASS_NO_LOGIN",
            axis2_permission_status="PERMITTED",
        )

@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires elevated privileges on Windows")
@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires elevated privileges on Windows")
def test_symlink_parent_detection(tmp_path: Path):
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    file_inside = real_dir / "file.txt"
    file_inside.write_text("hi")
    # create symlink to the directory
    link_dir = tmp_path / "linkdir"
    os.symlink(real_dir, link_dir)
    path_via_symlink = link_dir / "file.txt"
    with pytest.raises(ValueError):
        registry.register_file(
            path_via_symlink,
            domain_id="d",
            source_id="s",
            axis1_access_status="PASS_NO_LOGIN",
            axis2_permission_status="PERMITTED",
        )

# ---------- Quarantine Tests -------------------------------------------------

def test_successful_quarantine(tmp_path: Path):
    src = make_file(tmp_path)
    prov = registry.register_file(
        src,
        domain_id="qdom",
        source_id="qsrc",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    staged = Path(registry._repo_root()) / registry.STAGING_ROOT / "qdom" / "qsrc" / src.name
    quarantined = registry.quarantine_file(staged, reason="testreason")
    # Verify artifact moved
    assert not staged.exists()
    assert quarantined.is_file()
    # Verify provenance moved
    prov_path = quarantined.with_suffix('.provenance.json')
    assert prov_path.is_file()
    loaded = json.loads(prov_path.read_text())
    assert loaded == prov

def test_quarantine_invalid_reason(tmp_path: Path):
    src = make_file(tmp_path)
    prov = registry.register_file(
        src,
        domain_id="qr",
        source_id="sr",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    staged = Path(registry._repo_root()) / registry.STAGING_ROOT / "qr" / "sr" / src.name
    with pytest.raises(ValueError):
        registry.quarantine_file(staged, reason="bad reason!@#")

def test_quarantine_overwrite_protection(tmp_path: Path):
    src = make_file(tmp_path)
    prov = registry.register_file(
        src,
        domain_id="over",
        source_id="src",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    staged = Path(registry._repo_root()) / registry.STAGING_ROOT / "over" / "src" / src.name
    # First quarantine creates destination
    registry.quarantine_file(staged, reason="dup")
    # Re‑register same source (duplicate will fail, so copy another file)
    src2 = make_file(tmp_path, name="sample2.txt")
    prov2 = registry.register_file(
        src2,
        domain_id="over",
        source_id="src",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    staged2 = Path(registry._repo_root()) / registry.STAGING_ROOT / "over" / "src" / src2.name
    # Attempt quarantine with same reason should raise because dest exists
    with pytest.raises(FileExistsError):
        registry.quarantine_file(staged2, reason="dup")

# ---------- Promotion Tests ---------------------------------------------------

import errno
import hashlib


@pytest.fixture
def dummy_normalized(tmp_path: Path):
    file = tmp_path / "data.parquet"
    file.write_text("dummy content")
    return file


@pytest.fixture
def dummy_provenance(dummy_normalized: Path):
    def compute_hash(path: Path) -> str:
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()

    dummy_hash = compute_hash(dummy_normalized)
    prov = {
        "$schema": "https://nifty-quant.local/schemas/provenance-v1.json",
        "artifact_id": "test-id",
        "artifact_name": "data.parquet",
        "domain_id": "testdom",
        "source_id": "testsrc",
        "axis1_access_status": "PASS_NO_LOGIN",
        "axis2_permission_status": "PERMITTED",
        "source_sha256": "dummyhash",
        "normalized_parquet_sha256": dummy_hash,
        "raw_file_size_bytes": 123,
        "detected_schema_name": "UNKNOWN",
        "validation_engine_version": "1.0.0",
        "validation_severity_verdict": "PENDING",
        "validation_flags": [],
        "parent_lineage": [],
        "consent_record": None,
    }
    return prov


def _setup_mock_link_if_unsupported(monkeypatch, tmp_path: Path):
    """If the OS does not permit hard-link creation, mock os.link with atomic copy."""
    try:
        probe_src = tmp_path / ".probe_src"
        probe_dst = tmp_path / ".probe_dst"
        probe_src.write_text("p")
        os.link(probe_src, probe_dst)
        probe_dst.unlink()
        probe_src.unlink()
    except (PermissionError, OSError):
        def mock_link(src, dst, *args, **kwargs):
            if Path(dst).exists():
                raise FileExistsError(errno.EEXIST, f"File exists: {dst}")
            shutil.copy2(src, dst)
        monkeypatch.setattr(os, "link", mock_link)


def test_promotion_missing_axis1_fails(tmp_path: Path):
    src = make_file(tmp_path)
    prov = registry.register_file(
        src,
        domain_id="bad1",
        source_id="src",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    prov["axis1_access_status"] = None  # corrupt
    normalized = tmp_path / "norm.parquet"
    normalized.write_bytes(b"data")
    prov["normalized_parquet_sha256"] = registry.compute_sha256(normalized)
    with pytest.raises(ValueError):
        registry.promote_to_curated(normalized, prov)


def test_promotion_missing_normalized_hash_fails(tmp_path: Path):
    src = make_file(tmp_path)
    prov = registry.register_file(
        src,
        domain_id="bad2",
        source_id="src",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    normalized = tmp_path / "norm.parquet"
    normalized.write_bytes(b"data")
    # Do not set normalized_parquet_sha256
    with pytest.raises(ValueError):
        registry.promote_to_curated(normalized, prov)


def test_successful_promotion(tmp_path: Path, dummy_normalized, dummy_provenance, monkeypatch):
    _setup_mock_link_if_unsupported(monkeypatch, tmp_path)
    monkeypatch.setattr(registry, "_repo_root", lambda: tmp_path)
    dest_path = registry.promote_to_curated(dummy_normalized, dummy_provenance)

    # Artifact should exist and match source
    assert dest_path.is_file()
    assert dest_path.read_bytes() == dummy_normalized.read_bytes()

    # Provenance side-car should exist and contain matching data
    prov_path = dest_path.with_suffix(".provenance.json")
    assert prov_path.is_file()
    loaded = json.loads(prov_path.read_text())
    assert loaded["artifact_id"] == dummy_provenance["artifact_id"]

    # No temporary files remain
    dest_dir = dest_path.parent
    assert not any(p.name.startswith(".tmp_") for p in dest_dir.iterdir())


def test_promotion_overwrite_protection(tmp_path: Path, monkeypatch):
    _setup_mock_link_if_unsupported(monkeypatch, tmp_path)
    monkeypatch.setattr(registry, "_repo_root", lambda: tmp_path)
    src = make_file(tmp_path)
    prov = registry.register_file(
        src,
        domain_id="dupcur",
        source_id="src",
        axis1_access_status="PASS_NO_LOGIN",
        axis2_permission_status="PERMITTED",
    )
    normalized = tmp_path / "norm.parquet"
    normalized.write_bytes(b"data")
    prov["normalized_parquet_sha256"] = registry.compute_sha256(normalized)
    # First promotion creates target
    registry.promote_to_curated(normalized, prov)
    # Second promotion must raise due to existing file
    with pytest.raises(FileExistsError):
        registry.promote_to_curated(normalized, prov)


def test_promotion_existing_destination(tmp_path: Path, dummy_normalized, dummy_provenance, monkeypatch):
    monkeypatch.setattr(registry, "_repo_root", lambda: tmp_path)
    dest_dir = tmp_path / registry.CURATED_ROOT / dummy_provenance["domain_id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing_file = dest_dir / dummy_normalized.name
    existing_file.write_text("existing pre-created content")

    with pytest.raises(FileExistsError):
        registry.promote_to_curated(dummy_normalized, dummy_provenance)

    # Original file preserved unmodified
    assert existing_file.read_text() == "existing pre-created content"
    # No provenance sidecar created for the failed promotion
    assert not (dest_dir / (dummy_normalized.stem + ".provenance.json")).exists()
    # No temporary files left behind
    assert not any(p.name.startswith(".tmp_") for p in dest_dir.iterdir())


def test_promotion_race_condition(tmp_path: Path, dummy_normalized, dummy_provenance, monkeypatch):
    monkeypatch.setattr(registry, "_repo_root", lambda: tmp_path)
    dest_dir = tmp_path / registry.CURATED_ROOT / dummy_provenance["domain_id"]
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Simulate a competing process creating destination between existence check and link
    def fake_link(src, dst, *args, **kwargs):
        Path(dst).write_text("competing content from race process")
        raise OSError(errno.EEXIST, "File exists")

    monkeypatch.setattr(os, "link", fake_link)

    with pytest.raises(FileExistsError):
        registry.promote_to_curated(dummy_normalized, dummy_provenance)

    # Competing destination preserved
    dest_path = dest_dir / dummy_normalized.name
    assert dest_path.is_file()
    assert dest_path.read_text() == "competing content from race process"

    # No provenance side-car created
    prov_path = dest_path.with_suffix(".provenance.json")
    assert not prov_path.exists()

    # Temporary files cleaned up
    assert not any(p.name.startswith(".tmp_") for p in dest_dir.iterdir())


def test_promotion_hardlink_unsupported(tmp_path: Path, dummy_normalized, dummy_provenance, monkeypatch):
    """Verify that permission denial/unsupported hard-link fails closed with RuntimeError, distinguishing from FileExistsError."""
    monkeypatch.setattr(registry, "_repo_root", lambda: tmp_path)

    def link_denied(src, dst, *args, **kwargs):
        raise PermissionError("Access is denied (operation not permitted)")

    monkeypatch.setattr(os, "link", link_denied)

    with pytest.raises(RuntimeError) as exc_info:
        registry.promote_to_curated(dummy_normalized, dummy_provenance)

    assert "not supported" in str(exc_info.value).lower()

    # Destination file must NOT exist
    dest_dir = tmp_path / registry.CURATED_ROOT / dummy_provenance["domain_id"]
    assert not (dest_dir / dummy_normalized.name).exists()
    assert not (dest_dir / (dummy_normalized.stem + ".provenance.json")).exists()

    # Temporary files cleaned up
    if dest_dir.exists():
        assert not any(p.name.startswith(".tmp_") for p in dest_dir.iterdir())


def test_promotion_provenance_failure_rolls_back_artifact(tmp_path: Path, dummy_normalized, dummy_provenance, monkeypatch):
    """Verify that if provenance publication fails after artifact publication, the artifact is rolled back."""
    monkeypatch.setattr(registry, "_repo_root", lambda: tmp_path)

    def link_artifact_ok_prov_fail(src, dst, *args, **kwargs):
        if str(dst).endswith(".provenance.json"):
            raise OSError(errno.EIO, "Simulated I/O error on provenance link")
        shutil.copy2(src, dst)

    monkeypatch.setattr(os, "link", link_artifact_ok_prov_fail)

    with pytest.raises(FileExistsError):
        registry.promote_to_curated(dummy_normalized, dummy_provenance)

    dest_dir = tmp_path / registry.CURATED_ROOT / dummy_provenance["domain_id"]
    # Artifact created by this operation must be rolled back
    assert not (dest_dir / dummy_normalized.name).exists()
    assert not (dest_dir / (dummy_normalized.stem + ".provenance.json")).exists()

    # Temporary files cleaned up
    if dest_dir.exists():
        assert not any(p.name.startswith(".tmp_") for p in dest_dir.iterdir())

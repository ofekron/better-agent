from __future__ import annotations

import gzip
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "desktop"))

# pyxdelta shells out to the xdelta3 binary; skip cleanly where it is absent.
pytest.importorskip("update_delta")
pytest.importorskip("pyxdelta")

import update_delta  # noqa: E402
from tufup.client import SUFFIX_FAILED  # noqa: E402
from update_delta import (  # noqa: E402
    DELTA_FORMAT,
    TufupDeltaClient,
    _verify_tar,
    apply_patches,
    create_patch,
)


@dataclass(order=True)
class _PatchMeta:
    """Stand-in for tufup TargetMeta usable as a Mapping key."""

    version: str
    custom_internal: dict = field(default_factory=dict, compare=False)
    is_archive: bool = field(default=False, compare=False)
    is_patch: bool = field(default=True, compare=False)

    __hash__ = object.__hash__


def _archive(path: Path, content: bytes) -> None:
    with gzip.open(path, "wb") as archive:
        archive.write(content)


def _read_archive(path: Path) -> bytes:
    with gzip.open(path, "rb") as archive:
        return archive.read()


def test_cumulative_xdelta_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.tar.gz"
    middle = tmp_path / "middle.tar.gz"
    target = tmp_path / "target.tar.gz"
    first_patch = tmp_path / "middle.patch"
    second_patch = tmp_path / "target.patch"
    reconstructed = tmp_path / "reconstructed.tar.gz"

    base = bytearray((b"stable-block-" * 8192) + (b"A" * 2_000_000))
    changed = bytearray(base)
    changed[120_000:120_020] = b"first-version-change"
    final = bytearray(changed)
    final[1_500_000:1_500_020] = b"second-version-edit!"

    _archive(source, bytes(base))
    _archive(middle, bytes(changed))
    _archive(target, bytes(final))

    first_meta = create_patch(source, middle, first_patch)
    second_meta = create_patch(middle, target, second_patch)
    apply_patches(
        source,
        reconstructed,
        {
            _PatchMeta("1.1", first_meta): first_patch,
            _PatchMeta("1.2", second_meta): second_patch,
        },
    )

    assert first_meta["delta_format"] == DELTA_FORMAT
    assert second_meta["delta_format"] == DELTA_FORMAT
    assert _read_archive(reconstructed) == bytes(final)
    assert hashlib.sha256(bytes(final)).hexdigest() == second_meta["tar_hash"]


def test_apply_patches_rejects_empty_mapping(tmp_path: Path) -> None:
    source = tmp_path / "source.tar.gz"
    target = tmp_path / "target.tar.gz"
    _archive(source, b"source")

    with pytest.raises(ValueError, match="no desktop update patches"):
        apply_patches(source, target, {})


def test_apply_patches_rejects_unknown_delta_format(tmp_path: Path) -> None:
    source = tmp_path / "source.tar.gz"
    target = tmp_path / "target.tar.gz"
    patch = tmp_path / "target.patch"
    reconstructed = tmp_path / "reconstructed.tar.gz"
    _archive(source, b"source")
    _archive(target, b"target")
    metadata = create_patch(source, target, patch)
    metadata["delta_format"] = "unknown"

    with pytest.raises(ValueError, match="unsupported desktop update delta format"):
        apply_patches(
            source,
            reconstructed,
            {_PatchMeta("1.1", metadata): patch},
        )
    assert not reconstructed.exists()


def test_apply_patches_corrupt_patch_raises_without_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.tar.gz"
    target = tmp_path / "target.tar.gz"
    patch = tmp_path / "target.patch"
    reconstructed = tmp_path / "reconstructed.tar.gz"
    _archive(source, b"source")
    _archive(target, b"target")
    metadata = create_patch(source, target, patch)
    patch.write_bytes(b"not an xdelta patch")

    with pytest.raises(Exception):
        apply_patches(
            source,
            reconstructed,
            {_PatchMeta("1.1", metadata): patch},
        )
    assert not reconstructed.exists()


def test_verify_tar_accepts_valid_metadata_and_rejects_each_mismatch(
    tmp_path: Path,
) -> None:
    tar = tmp_path / "payload.tar"
    payload = b"hello-tar-contents"
    tar.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    base = {
        "delta_format": DELTA_FORMAT,
        "tar_hash_algorithm": "sha256",
        "tar_size": len(payload),
        "tar_hash": digest,
    }

    _verify_tar(tar, base)  # happy path: no raise

    wrong_format = dict(base, delta_format="bogus")
    with pytest.raises(ValueError, match="delta format"):
        _verify_tar(tar, wrong_format)

    wrong_algo = dict(base, tar_hash_algorithm="md5")
    with pytest.raises(ValueError, match="hash algorithm"):
        _verify_tar(tar, wrong_algo)

    wrong_size = dict(base, tar_size=len(payload) + 1)
    with pytest.raises(ValueError, match="tar size mismatch"):
        _verify_tar(tar, wrong_size)

    wrong_hash = dict(base, tar_hash="0" * 64)
    with pytest.raises(ValueError, match="tar hash mismatch"):
        _verify_tar(tar, wrong_hash)


def test_create_patch_raises_when_xdelta_generation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.tar.gz"
    target = tmp_path / "target.tar.gz"
    patch = tmp_path / "target.patch"
    _archive(source, b"source-data")
    _archive(target, b"target-data")

    monkeypatch.setattr(update_delta.pyxdelta, "run", lambda *_a: False)

    with pytest.raises(RuntimeError, match="patch generation failed"):
        create_patch(source, target, patch)
    assert not patch.exists()


def _stub_super(monkeypatch, captured: list):
    monkeypatch.setattr(
        "tufup.client.Client._apply_updates",
        lambda self, **kw: captured.append(kw) or "delegated",
    )


def _bare_client():
    return TufupDeltaClient.__new__(TufupDeltaClient)


def test_tufup_client_archive_target_delegates_to_super(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list = []
    _stub_super(monkeypatch, captured)

    client = _bare_client()
    client.downloaded_target_files = {
        _PatchMeta("2.0", is_archive=True, is_patch=False): tmp_path / "archive"
    }
    client.current_archive_local_path = tmp_path / "current.tar.gz"
    client.new_archive_local_path = tmp_path / "new.tar.gz"

    assert client._apply_updates(install=lambda: None, skip_confirmation=False) == "delegated"
    assert captured  # super()._apply_updates ran instead of patching


def test_tufup_client_empty_downloaded_delegates_to_super(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list = []
    _stub_super(monkeypatch, captured)

    client = _bare_client()
    client.downloaded_target_files = {}
    client.current_archive_local_path = tmp_path / "current.tar.gz"
    client.new_archive_local_path = tmp_path / "new.tar.gz"

    assert client._apply_updates(install=lambda: None, skip_confirmation=False) == "delegated"
    assert captured


def test_tufup_client_patch_path_rebuilds_archive_and_delegates(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list = []
    _stub_super(monkeypatch, captured)

    source = tmp_path / "source.tar.gz"
    target = tmp_path / "target.tar.gz"
    patch = tmp_path / "target.patch"
    _archive(source, (b"stable-segment-" * 200))
    _archive(target, (b"stable-segment-" * 199) + b"edited-segment-here")
    metadata = create_patch(source, target, patch)

    new_archive = tmp_path / "new.tar.gz"
    client = _bare_client()
    client.downloaded_target_files = {_PatchMeta("1.1", metadata, is_patch=True): patch}
    client.current_archive_local_path = source
    client.new_archive_local_path = new_archive

    assert client._apply_updates(install=lambda: None, skip_confirmation=False) == "delegated"
    assert new_archive.exists()
    assert _read_archive(new_archive) == _read_archive(target)
    assert captured


def test_tufup_client_patch_failure_renames_targets_and_returns_none(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # super() must NOT run when patching aborts.
    monkeypatch.setattr(
        "tufup.client.Client._apply_updates",
        lambda self, **_kw: pytest.fail("super()._apply_updates must not run on failure"),
    )

    source = tmp_path / "source.tar.gz"
    target = tmp_path / "target.tar.gz"
    patch = tmp_path / "target.patch"
    _archive(source, b"source")
    _archive(target, b"target")
    metadata = create_patch(source, target, patch)
    metadata["delta_format"] = "bogus"  # forces apply_patches to raise

    client = _bare_client()
    client.downloaded_target_files = {_PatchMeta("1.1", metadata, is_patch=True): patch}
    client.current_archive_local_path = source
    client.new_archive_local_path = tmp_path / "new.tar.gz"

    with caplog.at_level("ERROR", logger="update_delta"):
        result = client._apply_updates(install=lambda: None, skip_confirmation=False)

    assert result is None
    failed = patch.with_suffix(patch.suffix + SUFFIX_FAILED)
    assert failed.exists()
    assert not patch.exists()
    assert any("patching aborted" in record.message for record in caplog.records)


def test_tufup_client_mixed_targets_abort_renames_and_returns_none(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        "tufup.client.Client._apply_updates",
        lambda self, **_kw: pytest.fail("super()._apply_updates must not run on mix"),
    )

    patch_one = tmp_path / "one.patch"
    patch_two = tmp_path / "two.patch"
    patch_one.write_bytes(b"")
    patch_two.write_bytes(b"")

    client = _bare_client()
    client.downloaded_target_files = {
        _PatchMeta("1.1", is_patch=True): patch_one,
        _PatchMeta("1.0", is_patch=False, is_archive=False): patch_two,
    }
    client.current_archive_local_path = tmp_path / "current.tar.gz"
    client.new_archive_local_path = tmp_path / "new.tar.gz"

    with caplog.at_level("ERROR", logger="update_delta"):
        result = client._apply_updates(install=lambda: None, skip_confirmation=False)

    assert result is None
    assert any("mix archives and patches" in record.message for record in caplog.records)
    assert patch_one.with_suffix(patch_one.suffix + SUFFIX_FAILED).exists()
    assert patch_two.with_suffix(patch_two.suffix + SUFFIX_FAILED).exists()

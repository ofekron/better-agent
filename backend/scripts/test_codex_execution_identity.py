from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution_common import ExecutionContractError  # noqa: E402
from codex_execution_identity import (  # noqa: E402
    ConfigIdentity,
    FileIdentity,
    config_file_target_is_admissible,
    config_identity_from_dict,
    file_identity_from_dict,
    file_identity_to_dict,
)


def _write(path: Path, data: bytes = b"native") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --- FileIdentity.capture gates ---

def test_capture_rejects_relative_path() -> None:
    with pytest.raises(ExecutionContractError):
        FileIdentity.capture("relative/path")


def test_capture_rejects_non_regular_file() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw) / "a-dir"
        directory.mkdir()
        with pytest.raises(ExecutionContractError):
            FileIdentity.capture(directory)


def test_capture_translates_open_oserror_to_unavailable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "file"
        _write(target)

        def _boom(*_args, **_kwargs):
            raise OSError("injected")

        original = os.open
        os.open = _boom
        try:
            with pytest.raises(ExecutionContractError):
                FileIdentity.capture(target)
        finally:
            os.open = original


def test_capture_detects_file_changed_mid_capture() -> None:
    import codex_execution_identity as cei

    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "file"
        _write(target)
        # stable_stat_identity is consulted exactly twice inside _capture
        # (stat_before, stat_after). Differing values trip the TOCTOU guard.
        calls = {"n": 0}

        def _fake(_stat_result):
            calls["n"] += 1
            return (calls["n"],)

        original = cei.stable_stat_identity
        cei.stable_stat_identity = _fake
        try:
            with pytest.raises(ExecutionContractError):
                FileIdentity.capture(target)
        finally:
            cei.stable_stat_identity = original


def test_capture_detects_path_changed_mid_capture() -> None:
    import codex_execution_identity as cei

    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "file"
        _write(target)
        # symlink_chain is consulted twice (chain_before, chain_after); a
        # change trips the path-identity TOCTOU guard.
        calls = {"n": 0}

        def _fake(_path):
            calls["n"] += 1
            return () if calls["n"] == 1 else (("/link", "target"),)

        original = cei.symlink_chain
        cei.symlink_chain = _fake
        try:
            with pytest.raises(ExecutionContractError):
                FileIdentity.capture(target)
        finally:
            cei.symlink_chain = original


# --- capture_with_first_line (cold + warm cache paths) ---

def test_capture_with_first_line_cold_then_warm() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "launcher"
        _write(target, b"#!/usr/bin/env node\nbody\n")
        cold_identity, cold_line = FileIdentity.capture_with_first_line(target)
        assert cold_line == b"#!/usr/bin/env node\n"
        warm_identity, warm_line = FileIdentity.capture_with_first_line(target)
        assert warm_identity == cold_identity
        assert warm_line == cold_line


# --- attest / attest_metadata reason branches ---

def test_attest_with_reason_reports_changed_then_unavailable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "file"
        _write(target, b"v1")
        identity = FileIdentity.capture(target)

        assert identity.attest() is True
        _write(target, b"v2-different-content")
        ok, reason = identity.attest_with_reason()
        assert ok is False
        assert reason.startswith("file_changed:")

        target.unlink()
        ok, reason = identity.attest_with_reason()
        assert ok is False
        assert reason.startswith("file_unavailable:")


def test_attest_metadata_with_reason_reports_unavailable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "file"
        _write(target)
        identity = FileIdentity.capture(target)
        assert identity.attest_metadata() is True
        target.unlink()
        ok, reason = identity.attest_metadata_with_reason()
        assert ok is False
        assert reason.startswith("file_metadata_unavailable:")


# --- capture_metadata gates ---

def test_capture_metadata_rejects_relative_path() -> None:
    with pytest.raises(ExecutionContractError):
        FileIdentity.capture_metadata("relative/path")


def test_capture_metadata_translates_missing_path_oserror() -> None:
    with tempfile.TemporaryDirectory() as raw:
        with pytest.raises(ExecutionContractError):
            FileIdentity.capture_metadata(Path(raw) / "missing")


def test_capture_metadata_rejects_non_file() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw) / "a-dir"
        directory.mkdir()
        with pytest.raises(ExecutionContractError):
            FileIdentity.capture_metadata(directory)


# --- file_identity_to_dict / from_dict ---

def test_file_identity_to_dict_serializes_symlink_chain_as_lists() -> None:
    identity = FileIdentity(
        requested_path="/a/b",
        resolved_path="/a/b",
        sha256="a" * 64,
        size=1,
        mtime_ns=2,
        ctime_ns=3,
        device=4,
        inode=5,
        symlink_chain=(("/a/link", "target"),),
    )
    encoded = file_identity_to_dict(identity)
    assert encoded["symlink_chain"] == [["/a/link", "target"]]
    assert file_identity_from_dict(encoded) == identity


def test_file_identity_from_dict_rejects_semantic_violations() -> None:
    base = file_identity_to_dict(
        FileIdentity(
            requested_path="/a/b",
            resolved_path="/a/b",
            sha256="a" * 64,
            size=1,
            mtime_ns=2,
            ctime_ns=3,
            device=4,
            inode=5,
        )
    )
    relative = dict(base, requested_path="relative")
    with pytest.raises(ExecutionContractError):
        file_identity_from_dict(relative)

    bad_sha = dict(base, sha256="not-a-hex")
    with pytest.raises(ExecutionContractError):
        file_identity_from_dict(bad_sha)

    negative = dict(base, size=-1)
    with pytest.raises(ExecutionContractError):
        file_identity_from_dict(negative)


def test_file_identity_from_dict_rejects_malformed_symlink_chain() -> None:
    base = file_identity_to_dict(
        FileIdentity(
            requested_path="/a/b",
            resolved_path="/a/b",
            sha256="a" * 64,
            size=1,
            mtime_ns=2,
            ctime_ns=3,
            device=4,
            inode=5,
        )
    )
    base["symlink_chain"] = "not-a-list"
    with pytest.raises(ExecutionContractError):
        file_identity_from_dict(base)


# --- config_file_target_is_admissible branches ---

def test_config_file_target_is_admissible_branches() -> None:
    inside = FileIdentity(
        requested_path="/root/cfg",
        resolved_path="/root/cfg",
        sha256="a" * 64,
        size=1,
        mtime_ns=2,
        ctime_ns=3,
        device=4,
        inode=5,
    )
    # Resolved path lives under the root -> admissible.
    assert config_file_target_is_admissible("/root", "/root/cfg", inside) is True

    # requested_path disagrees with the candidate -> not admissible.
    assert (
        config_file_target_is_admissible("/root", "/root/other", inside) is False
    )

    # Resolved path escapes the root, but the symlink chain anchors it to the
    # requested candidate -> admissible via the chain fallback.
    escaped = FileIdentity(
        requested_path="/root/cfg",
        resolved_path="/outside/cfg",
        sha256="a" * 64,
        size=1,
        mtime_ns=2,
        ctime_ns=3,
        device=4,
        inode=5,
        symlink_chain=(("/root/cfg", "/outside/cfg"),),
    )
    assert config_file_target_is_admissible("/root", "/root/cfg", escaped) is True

    # Escaped with no anchoring chain -> not admissible.
    escaped_no_chain = FileIdentity(
        requested_path="/root/cfg",
        resolved_path="/outside/cfg",
        sha256="a" * 64,
        size=1,
        mtime_ns=2,
        ctime_ns=3,
        device=4,
        inode=5,
    )
    assert (
        config_file_target_is_admissible("/root", "/root/cfg", escaped_no_chain)
        is False
    )


# --- ConfigIdentity.capture gates ---
#
# macOS temp dirs live under /var (a symlink to /private/var). ConfigIdentity
# stores a resolved root_path but a lexical config_path, so the two diverge
# unless every test path is built from an already-resolved base. Resolve once.

def test_config_capture_rejects_relative_paths() -> None:
    with pytest.raises(ExecutionContractError):
        ConfigIdentity.capture("relative/root", "/abs/cfg")


def test_config_capture_rejects_unavailable_root() -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw).resolve()
        with pytest.raises(ExecutionContractError):
            ConfigIdentity.capture(base / "missing", base / "missing" / "cfg")


def test_config_capture_rejects_unavailable_parent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        with pytest.raises(ExecutionContractError):
            ConfigIdentity.capture(root, root / "missing-parent" / "cfg")


def test_config_capture_rejects_path_escaping_root() -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw).resolve()
        root = base / "root"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        # A symlinked PARENT resolves outside the root -> the resolved parent
        # is not relative to the canonical root, so capture rejects it.
        escaping_parent = root / "subdir"
        escaping_parent.symlink_to(outside)
        with pytest.raises(ExecutionContractError):
            ConfigIdentity.capture(root, escaping_parent / "cfg")


def test_config_capture_allows_missing_config_file() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        identity = ConfigIdentity.capture(root, root / "absent.cfg")
        assert identity.config_file is None


# --- ConfigIdentity.attest branches ---

def test_config_attest_reports_file_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        cfg = root / "config.toml"
        _write(cfg, b"v1")
        identity = ConfigIdentity.capture(root, cfg)
        assert identity.attest() is True
        _write(cfg, b"v2-different")
        ok, reason = identity.attest_with_reason()
        assert ok is False
        assert reason.startswith("config_file_mismatch:")


def test_config_attest_reports_capture_failure_for_broken_symlink() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        cfg = root / "config.toml"
        _write(cfg, b"v1")
        identity = ConfigIdentity.capture(root, cfg)
        cfg.unlink()
        cfg.symlink_to(root / "does-not-exist")
        ok, reason = identity.attest_with_reason()
        assert ok is False
        assert reason.startswith("config_capture_failed:")


def test_config_attest_reports_oserror_when_parent_missing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        nested = root / "nested"
        nested.mkdir()
        cfg = nested / "config.toml"
        _write(cfg, b"v1")
        identity = ConfigIdentity.capture(root, cfg)
        cfg.unlink()
        nested.rmdir()
        ok, reason = identity.attest_with_reason()
        assert ok is False
        assert reason.startswith("config_oserror:")


# --- ConfigIdentity.attest_metadata branches ---

def test_config_attest_metadata_reports_unexpectedly_existing_file() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        cfg = root / "absent.cfg"
        identity = ConfigIdentity.capture(root, cfg)
        assert identity.attest_metadata() is True
        _write(cfg, b"now-present")
        ok, reason = identity.attest_metadata_with_reason()
        assert ok is False
        assert reason.startswith("config_file_unexpectedly_exists:")


def test_config_attest_metadata_reports_parent_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        nested = root / "nested"
        nested.mkdir()
        cfg = nested / "config.toml"
        _write(cfg, b"v1")
        identity = ConfigIdentity.capture(root, cfg)
        # Recreating the parent yields a new inode -> parent routing mismatch.
        cfg.unlink()
        nested.rmdir()
        nested.mkdir()
        _write(cfg, b"v1")
        ok, reason = identity.attest_metadata_with_reason()
        assert ok is False
        assert reason.startswith("config_parent_metadata_mismatch:")


def test_config_attest_metadata_reports_oserror_when_parent_missing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        nested = root / "nested"
        nested.mkdir()
        cfg = nested / "config.toml"
        _write(cfg, b"v1")
        identity = ConfigIdentity.capture(root, cfg)
        cfg.unlink()
        nested.rmdir()
        ok, reason = identity.attest_metadata_with_reason()
        assert ok is False
        assert reason.startswith("config_metadata_oserror:")


def test_config_capture_detects_parent_changed_mid_capture() -> None:
    import codex_execution_identity as cei

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        # A missing config file skips the inner FileIdentity.capture, so
        # stable_stat_identity is consulted exactly twice (parent_before /
        # parent_after); differing values trip the parent TOCTOU guard.
        calls = {"n": 0}

        def _fake(_stat_result):
            calls["n"] += 1
            return (calls["n"],)

        original = cei.stable_stat_identity
        cei.stable_stat_identity = _fake
        try:
            with pytest.raises(ExecutionContractError):
                ConfigIdentity.capture(root, root / "absent.cfg")
        finally:
            cei.stable_stat_identity = original


def test_config_attest_reports_root_resolved_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw).resolve()
        real = base / "real"
        real.mkdir()
        link = base / "link"
        link.symlink_to(real)
        st = real.stat()
        # A config identity whose root_path is an unresolved symlink: capture
        # canonicalizes root so it can never produce this, but from_dict
        # accepts it (paths are lexically consistent, no config file). Attest
        # then sees root.resolve() disagree with the stored root.
        payload = {
            "root_path": str(link),
            "parent_path": str(link),
            "parent_mode": st.st_mode,
            "parent_size": st.st_size,
            "parent_mtime_ns": st.st_mtime_ns,
            "parent_ctime_ns": st.st_ctime_ns,
            "parent_device": st.st_dev,
            "parent_inode": st.st_ino,
            "config_path": str(link / "cfg"),
            "config_file": None,
        }
        identity = config_identity_from_dict(payload)
        ok, reason = identity.attest_with_reason()
        assert ok is False
        assert reason.startswith("config_root_resolved_mismatch:")
        ok_meta, reason_meta = identity.attest_metadata_with_reason()
        assert ok_meta is False
        assert reason_meta.startswith("config_root_resolved_mismatch:")


# --- config_identity_from_dict ---

def _valid_config_dict(root: Path, cfg: Path) -> dict:
    import json
    from dataclasses import asdict

    # Canonical serialized form: asdict leaves symlink_chain as a tuple, but
    # config_identity_from_dict requires lists. JSON round-trip normalizes.
    return json.loads(json.dumps(asdict(ConfigIdentity.capture(root, cfg))))


def test_config_identity_from_dict_rejects_set_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        cfg = root / "config.toml"
        _write(cfg)
        encoded = _valid_config_dict(root, cfg)
    encoded["unexpected"] = 1
    with pytest.raises(ExecutionContractError):
        config_identity_from_dict(encoded)


def test_config_identity_from_dict_rejects_semantic_violations() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        cfg = root / "config.toml"
        _write(cfg)
        encoded = _valid_config_dict(root, cfg)

    relative_root = dict(encoded, root_path="relative")
    with pytest.raises(ExecutionContractError):
        config_identity_from_dict(relative_root)

    wrong_parent = dict(encoded, parent_path=str(root.parent / "elsewhere"))
    with pytest.raises(ExecutionContractError):
        config_identity_from_dict(wrong_parent)

    negative = dict(encoded, parent_size=-1)
    with pytest.raises(ExecutionContractError):
        config_identity_from_dict(negative)


def test_config_identity_from_dict_rejects_non_dict_config_file() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        cfg = root / "config.toml"
        _write(cfg)
        encoded = _valid_config_dict(root, cfg)
    encoded["config_file"] = "not-a-dict"
    with pytest.raises(ExecutionContractError):
        config_identity_from_dict(encoded)


def test_config_identity_from_dict_rejects_non_admissible_config_file() -> None:
    inside = file_identity_to_dict(
        FileIdentity(
            requested_path="/root/cfg",
            resolved_path="/outside/cfg",
            sha256="a" * 64,
            size=1,
            mtime_ns=2,
            ctime_ns=3,
            device=4,
            inode=5,
        )
    )
    payload = {
        "root_path": "/root",
        "parent_path": "/root",
        "parent_mode": stat.S_IFDIR | 0o755,
        "parent_size": 1,
        "parent_mtime_ns": 2,
        "parent_ctime_ns": 3,
        "parent_device": 4,
        "parent_inode": 5,
        "config_path": "/root/cfg",
        "config_file": inside,
    }
    with pytest.raises(ExecutionContractError):
        config_identity_from_dict(payload)


def test_config_identity_round_trips_through_from_dict() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "root"
        root.mkdir()
        cfg = root / "config.toml"
        _write(cfg, b"content")
        identity = ConfigIdentity.capture(root, cfg)
        encoded = _valid_config_dict(root, cfg)
    assert config_identity_from_dict(encoded) == identity


IDENTITY_TESTS = (
    test_capture_rejects_relative_path,
    test_capture_rejects_non_regular_file,
    test_capture_translates_open_oserror_to_unavailable,
    test_capture_detects_file_changed_mid_capture,
    test_capture_detects_path_changed_mid_capture,
    test_capture_with_first_line_cold_then_warm,
    test_attest_with_reason_reports_changed_then_unavailable,
    test_attest_metadata_with_reason_reports_unavailable,
    test_capture_metadata_rejects_relative_path,
    test_capture_metadata_translates_missing_path_oserror,
    test_capture_metadata_rejects_non_file,
    test_file_identity_to_dict_serializes_symlink_chain_as_lists,
    test_file_identity_from_dict_rejects_semantic_violations,
    test_file_identity_from_dict_rejects_malformed_symlink_chain,
    test_config_file_target_is_admissible_branches,
    test_config_capture_rejects_relative_paths,
    test_config_capture_rejects_unavailable_root,
    test_config_capture_rejects_unavailable_parent,
    test_config_capture_rejects_path_escaping_root,
    test_config_capture_allows_missing_config_file,
    test_config_attest_reports_file_mismatch,
    test_config_attest_reports_capture_failure_for_broken_symlink,
    test_config_attest_reports_oserror_when_parent_missing,
    test_config_attest_metadata_reports_unexpectedly_existing_file,
    test_config_attest_metadata_reports_parent_mismatch,
    test_config_attest_metadata_reports_oserror_when_parent_missing,
    test_config_capture_detects_parent_changed_mid_capture,
    test_config_attest_reports_root_resolved_mismatch,
    test_config_identity_from_dict_rejects_set_mismatch,
    test_config_identity_from_dict_rejects_semantic_violations,
    test_config_identity_from_dict_rejects_non_dict_config_file,
    test_config_identity_from_dict_rejects_non_admissible_config_file,
    test_config_identity_round_trips_through_from_dict,
)


if __name__ == "__main__":
    for test in IDENTITY_TESTS:
        test()
    print("PASS codex execution identity")


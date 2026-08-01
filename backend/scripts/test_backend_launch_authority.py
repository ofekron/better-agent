"""Unit coverage for backend_launch_authority.

Exercises the launch-authority issue/validate/sanitize cycle in-process against
an isolated temp home. The non-test-mode validation path is reached by clearing
``BETTER_AGENT_TEST_MODE`` per-test (conftest arms it session-wide); home stays a
pytest tempdir, never production state.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import backend_launch_authority as bla

_TOKEN_ENV = bla._TOKEN_ENV
_GENERATION_ENV = bla._GENERATION_ENV
_ACTIVE_ENV = bla._ACTIVE_CHECKOUT_ENV
_AUTHORITY_FILE = bla._AUTHORITY_FILE


# --- helpers ---------------------------------------------------------------


def _authority_path(root: Path) -> Path:
    return root / _AUTHORITY_FILE


def _read_payload(root: Path) -> dict:
    return json.loads(_authority_path(root).read_text())


def _write_payload(root: Path, payload: dict) -> None:
    _authority_path(root).write_text(json.dumps(payload))


def _tamper(root: Path, **overrides: object) -> dict:
    payload = _read_payload(root)
    payload.update(overrides)
    _write_payload(root, payload)
    return payload


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    return path


@pytest.fixture
def real_home(tmp_path: Path, monkeypatch) -> Path:
    """Point ba_home() at a tempdir and DISABLE test mode so the full
    validation path runs (the conftest sentinel short-circuits it otherwise)."""
    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("BETTER_AGENT_TEST_MODE", raising=False)
    return tmp_path


@pytest.fixture
def authorized(real_home: Path, checkout: Path, monkeypatch) -> tuple[Path, Path]:
    """Issue a valid authority record and populate the launch env."""
    env = bla.issue_primary_backend_launch(
        checkout=checkout, state_root=real_home
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return real_home, checkout


# --- launch_env_keys / _token_digest --------------------------------------


def test_launch_env_keys_shape() -> None:
    keys = bla.launch_env_keys()
    assert set(keys) == {
        _TOKEN_ENV,
        _GENERATION_ENV,
        _ACTIVE_ENV,
        "BETTER_CLAUDE_ACTIVE_CHECKOUT",
    }


def test_token_digest_is_sha256_hex() -> None:
    expected = hashlib.sha256(b"secret-token").hexdigest()
    assert bla._token_digest("secret-token") == expected
    assert len(expected) == 64


# --- _canonical ------------------------------------------------------------


def test_canonical_resolves_absolute(tmp_path: Path) -> None:
    assert bla._canonical(str(tmp_path)) == tmp_path.resolve()


@pytest.mark.parametrize("bad", ["relative/path", "../escape"])
def test_canonical_rejects_non_absolute(bad: str) -> None:
    with pytest.raises(RuntimeError, match="absolute"):
        bla._canonical(bad)


def test_canonical_rejects_dotdot_segments() -> None:
    with pytest.raises(RuntimeError, match="absolute"):
        bla._canonical("/etc/../etc")


# --- issue_primary_backend_launch -----------------------------------------


def test_issue_writes_record_and_returns_env(
    real_home: Path, checkout: Path
) -> None:
    env = bla.issue_primary_backend_launch(checkout=checkout, state_root=real_home)
    assert set(env) == {
        _TOKEN_ENV,
        _GENERATION_ENV,
        _ACTIVE_ENV,
        "BETTER_CLAUDE_ACTIVE_CHECKOUT",
    }
    assert bla._HEX_32_RE.fullmatch(env[_GENERATION_ENV])
    payload = _read_payload(real_home)
    assert set(payload) == bla._RECORD_KEYS
    assert payload["version"] == bla._VERSION
    assert payload["role"] == "primary"
    assert payload["checkout"] == str(checkout.resolve())
    assert payload["state_root"] == str(real_home.resolve())
    assert payload["token_sha256"] == bla._token_digest(env[_TOKEN_ENV])
    assert payload["generation"] == env[_GENERATION_ENV]
    assert isinstance(payload["issuer_pid"], int)
    assert isinstance(payload["issued_at"], float)


def test_issue_honors_explicit_generation(real_home: Path, checkout: Path) -> None:
    gen = "a" * 32
    env = bla.issue_primary_backend_launch(
        checkout=checkout, state_root=real_home, generation=gen
    )
    assert env[_GENERATION_ENV] == gen


@pytest.mark.parametrize("bad", ["short", "z" * 32, "g" * 31, "G" * 32])
def test_issue_rejects_invalid_generation(
    real_home: Path, checkout: Path, bad: str
) -> None:
    with pytest.raises(ValueError, match="32 lowercase hex"):
        bla.issue_primary_backend_launch(
            checkout=checkout, state_root=real_home, generation=bad
        )


def test_issue_defaults_state_root_to_ba_home(
    real_home: Path, checkout: Path
) -> None:
    env = bla.issue_primary_backend_launch(checkout=checkout)
    assert _authority_path(real_home).exists()
    assert env[_ACTIVE_ENV] == str(checkout.resolve())


# --- _write_private_json ---------------------------------------------------


def test_write_private_json_atomic_and_private(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "out.json"
    bla._write_private_json(target, {"b": 2, "a": 1})
    # Sorted-keys canonical JSON.
    assert target.read_text() == '{"a":1,"b":2}'
    # Owner-only perms on posix.
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # No leftover temp file.
    assert not list((tmp_path / "nested").glob(".*.tmp"))


def test_write_private_json_closes_fd_on_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "out.json"

    def fail_write(fd: int, data: bytes) -> int:
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError):
        bla._write_private_json(target, {"x": 1})
    # The fd opened before the failed write was closed by the finally clause,
    # and the temp file was unlinked; the target never landed.
    assert not target.exists()


def test_write_private_json_replaces_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_text("old")
    bla._write_private_json(target, {"v": 9})
    assert json.loads(target.read_text()) == {"v": 9}


# --- _read_object ----------------------------------------------------------


def test_read_object_returns_dict(tmp_path: Path) -> None:
    path = tmp_path / "rec.json"
    bla._write_private_json(path, {"k": "v"})
    assert bla._read_object(path, "record") == {"k": "v"}


def test_read_object_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="record is missing"):
        bla._read_object(tmp_path / "nope.json", "record")


def test_read_object_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    bla._write_private_json(real, {"k": 1})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(RuntimeError, match="record is invalid"):
        bla._read_object(link, "record")


def test_read_object_rejects_non_regular(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="record is invalid"):
        bla._read_object(tmp_path, "record")  # a directory


def test_read_object_open_failure_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "locked.json"
    bla._write_private_json(path, {"k": 1})
    os.chmod(path, 0o000)
    try:
        with pytest.raises(RuntimeError, match="record is invalid"):
            bla._read_object(path, "record")
    finally:
        os.chmod(path, 0o600)


def test_read_object_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(b"\xff\xfe not json")
    with pytest.raises(RuntimeError, match="record is invalid"):
        bla._read_object(path, "record")


def test_read_object_rejects_non_dict_json(tmp_path: Path) -> None:
    path = tmp_path / "arr.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(RuntimeError, match="record is invalid"):
        bla._read_object(path, "record")


# --- _validate_pointer -----------------------------------------------------


def _write_pointer(root: Path, payload: dict) -> None:
    (root / "active_checkout.json").write_text(json.dumps(payload))


def test_validate_pointer_absent_is_ok(real_home: Path, checkout: Path) -> None:
    # No active_checkout.json -> returns without checking.
    bla._validate_pointer(real_home, checkout.resolve())


def test_validate_pointer_failed_status_ok(real_home: Path, checkout: Path) -> None:
    _write_pointer(real_home, {"status": "failed", "active": str(checkout)})
    bla._validate_pointer(real_home, checkout.resolve())


@pytest.mark.parametrize("status", ["active", "switching", "reverted"])
def test_validate_pointer_matching_active_ok(
    real_home: Path, checkout: Path, status: str
) -> None:
    _write_pointer(real_home, {"status": status, "active": str(checkout.resolve())})
    bla._validate_pointer(real_home, checkout.resolve())


def test_validate_pointer_unknown_status_rejected(
    real_home: Path, checkout: Path
) -> None:
    _write_pointer(real_home, {"status": "unknown", "active": str(checkout.resolve())})
    with pytest.raises(RuntimeError, match="pointer is invalid"):
        bla._validate_pointer(real_home, checkout.resolve())


def test_validate_pointer_empty_active_rejected(
    real_home: Path, checkout: Path
) -> None:
    _write_pointer(real_home, {"status": "active", "active": ""})
    with pytest.raises(RuntimeError, match="pointer is invalid"):
        bla._validate_pointer(real_home, checkout.resolve())


def test_validate_pointer_non_string_active_rejected(
    real_home: Path, checkout: Path
) -> None:
    _write_pointer(real_home, {"status": "active", "active": 5})
    with pytest.raises(RuntimeError, match="pointer is invalid"):
        bla._validate_pointer(real_home, checkout.resolve())


def test_validate_pointer_active_mismatch_rejected(
    real_home: Path, checkout: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _write_pointer(real_home, {"status": "active", "active": str(other.resolve())})
    with pytest.raises(RuntimeError, match="does not match active checkout"):
        bla._validate_pointer(real_home, checkout.resolve())


# --- _record_path ----------------------------------------------------------


def test_record_path_valid(tmp_path: Path) -> None:
    assert bla._record_path({"checkout": str(tmp_path)}, "checkout") == tmp_path.resolve()


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"checkout": ""},
        {"checkout": 5},
        {"checkout": None},
    ],
)
def test_record_path_invalid(record: dict) -> None:
    with pytest.raises(RuntimeError, match="invalid checkout"):
        bla._record_path(record, "checkout")


def test_record_path_rejects_relative() -> None:
    with pytest.raises(RuntimeError, match="absolute"):
        bla._record_path({"checkout": "rel/path"}, "checkout")


# --- assert_primary_backend_launch_authorized ------------------------------


def test_assert_test_mode_shortcut(tmp_path: Path, checkout: Path, monkeypatch) -> None:
    # Test mode (sentinel armed by conftest) short-circuits to an empty authority.
    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    authority = bla.assert_primary_backend_launch_authorized(
        executing_checkout=checkout
    )
    assert authority.test_mode is True
    assert authority.token == ""
    assert authority.generation == ""
    assert authority.checkout == checkout.resolve()
    assert authority.state_root == tmp_path.resolve()


def test_assert_missing_env_rejected(real_home: Path, checkout: Path) -> None:
    with pytest.raises(RuntimeError, match="environment is missing"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_env_checkout_mismatch_rejected(
    authorized: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    real_home, checkout = authorized
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv(_ACTIVE_ENV, str(other.resolve()))
    with pytest.raises(RuntimeError, match="checkout does not match executable"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_record_unexpected_fields_rejected(
    authorized: tuple[Path, Path]
) -> None:
    real_home, checkout = authorized
    _tamper(real_home, surprise=True)
    with pytest.raises(RuntimeError, match="unexpected fields"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_record_missing_field_rejected(
    authorized: tuple[Path, Path]
) -> None:
    real_home, checkout = authorized
    payload = _read_payload(real_home)
    del payload["issued_at"]
    _write_payload(real_home, payload)
    with pytest.raises(RuntimeError, match="unexpected fields"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


@pytest.mark.parametrize(
    "field, value",
    [
        ("version", 2),
        ("role", "secondary"),
        ("issuer_pid", "x"),
        ("issuer_pid", True),
        ("issuer_pid", 0),
        ("issued_at", "x"),
        ("issued_at", True),
        ("generation", "z" * 32),
        ("token_sha256", "short"),
        ("token_sha256", 5),
    ],
)
def test_assert_record_field_validation_rejected(
    authorized: tuple[Path, Path], field: str, value: object
) -> None:
    real_home, checkout = authorized
    _tamper(real_home, **{field: value})
    with pytest.raises(RuntimeError, match="record is invalid"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_stale_generation_rejected(
    authorized: tuple[Path, Path], monkeypatch
) -> None:
    real_home, checkout = authorized
    monkeypatch.setenv(_GENERATION_ENV, "b" * 32)
    with pytest.raises(RuntimeError, match="generation is stale"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_stale_token_rejected(authorized: tuple[Path, Path], monkeypatch) -> None:
    real_home, checkout = authorized
    monkeypatch.setenv(_TOKEN_ENV, "wrong-token")
    with pytest.raises(RuntimeError, match="token is stale"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_record_checkout_mismatch_rejected(
    authorized: tuple[Path, Path], tmp_path: Path
) -> None:
    real_home, checkout = authorized
    other = tmp_path / "alt-checkout"
    other.mkdir()
    _tamper(real_home, checkout=str(other.resolve()))
    with pytest.raises(RuntimeError, match="record checkout does not match"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_record_state_root_mismatch_rejected(
    authorized: tuple[Path, Path], tmp_path: Path
) -> None:
    real_home, checkout = authorized
    other = tmp_path / "alt-root"
    other.mkdir()
    _tamper(real_home, state_root=str(other.resolve()))
    with pytest.raises(RuntimeError, match="record state root does not match"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


def test_assert_happy_path(authorized: tuple[Path, Path]) -> None:
    real_home, checkout = authorized
    authority = bla.assert_primary_backend_launch_authorized(
        executing_checkout=checkout
    )
    assert authority.test_mode is False
    assert authority.generation == os.environ[_GENERATION_ENV]
    assert authority.token == os.environ[_TOKEN_ENV]
    assert authority.checkout == checkout.resolve()
    assert authority.state_root == real_home.resolve()


def test_assert_validates_pointer_on_happy_path(
    authorized: tuple[Path, Path]
) -> None:
    real_home, checkout = authorized
    _write_pointer(
        real_home, {"status": "active", "active": str(checkout.resolve())}
    )
    authority = bla.assert_primary_backend_launch_authorized(
        executing_checkout=checkout
    )
    assert authority.test_mode is False


def test_assert_rejects_when_pointer_mismatch(authorized: tuple[Path, Path]) -> None:
    real_home, checkout = authorized
    _write_pointer(real_home, {"status": "unknown", "active": str(checkout.resolve())})
    with pytest.raises(RuntimeError, match="pointer is invalid"):
        bla.assert_primary_backend_launch_authorized(executing_checkout=checkout)


# --- clear_primary_backend_launch_token ------------------------------------


def test_clear_noop_in_test_mode(tmp_path: Path, checkout: Path, monkeypatch) -> None:
    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    authority = bla.assert_primary_backend_launch_authorized(
        executing_checkout=checkout
    )
    assert authority.test_mode is True
    monkeypatch.setenv(_TOKEN_ENV, "anything")
    bla.clear_primary_backend_launch_token(authority)  # must not raise
    assert _TOKEN_ENV in os.environ  # not cleared in test mode


def test_clear_rejects_env_token_drift(authorized: tuple[Path, Path], monkeypatch) -> None:
    real_home, checkout = authorized
    authority = bla.assert_primary_backend_launch_authorized(
        executing_checkout=checkout
    )
    monkeypatch.setenv(_TOKEN_ENV, "different")
    with pytest.raises(RuntimeError, match="token changed before sanitization"):
        bla.clear_primary_backend_launch_token(authority)


def test_clear_pops_token(authorized: tuple[Path, Path]) -> None:
    real_home, checkout = authorized
    authority = bla.assert_primary_backend_launch_authorized(
        executing_checkout=checkout
    )
    assert _TOKEN_ENV in os.environ
    bla.clear_primary_backend_launch_token(authority)
    assert _TOKEN_ENV not in os.environ

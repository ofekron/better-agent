"""Unit coverage for run_execution_payload_manifest.py (the pure parser).

A separate full-stack script (test_run_execution_payload_manifest.py) exercises
the higher-level run_execution_payloads publication flow against a real
filesystem. This file is the UNIT-tier complement: it drives the strict parser
``parse_manifest`` and its helpers directly, covering every acceptance and
rejection branch (encoding, schema, version, run-directory, field validation,
file schema/field, integer validation, canonical ordering, duplicate files,
duplicate JSON keys, and canonical-bytes mismatch) without any filesystem or
subprocess dependency. Each test asserts one distinguishing outcome.
"""

from __future__ import annotations

import json
import os

import pytest
from codex_execution_common import ExecutionContractError

from run_execution_payload_manifest import (
    MANIFEST_VERSION,
    ExecutionPayloadManifest,
    PayloadFile,
    canonical_bytes,
    parse_manifest,
    payload_file_mode,
)

HEX64 = "a" * 64
FP = "b" * 64


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _file_dict(name: str = "payload.tar") -> dict:
    return {
        "ctime_ns": 1,
        "device": 2,
        "inode": 3,
        "kind": "regular_file",
        "mode": payload_file_mode(),
        "mtime_ns": 4,
        "name": name,
        "sha256": HEX64,
        "size": 5,
    }


def _manifest_dict(files=None) -> dict:
    return {
        "files": [_file_dict()] if files is None else files,
        "payload_root": "claude-cli",
        "payload_root_fingerprint": FP,
        "run_directory": {"device": 7, "inode": 8},
        "run_id": "run-42",
        "version": MANIFEST_VERSION,
    }


def _manifest() -> ExecutionPayloadManifest:
    file = PayloadFile(
        name="payload.tar",
        kind="regular_file",
        mode=payload_file_mode(),
        size=5,
        mtime_ns=4,
        ctime_ns=1,
        device=2,
        inode=3,
        sha256=HEX64,
    )
    return ExecutionPayloadManifest(
        run_id="run-42",
        run_directory_device=7,
        run_directory_inode=8,
        payload_root="claude-cli",
        payload_root_fingerprint=FP,
        files=(file,),
    )


def _encode(d: dict) -> bytes:
    """Canonical encoding, mirroring canonical_bytes over as_dict()."""
    return (
        json.dumps(d, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _encoded_with(**overrides) -> bytes:
    base = _manifest_dict()
    base.update(overrides)
    return _encode(base)


def _encoded_with_files(files) -> bytes:
    return _encode(_manifest_dict(files=files))


# ---------------------------------------------------------------------------
# happy path, canonical determinism, as_dict shape
# ---------------------------------------------------------------------------


def test_module_version_is_2():
    assert MANIFEST_VERSION == 2


def test_round_trip_canonical_manifest():
    manifest = _manifest()
    assert parse_manifest(canonical_bytes(manifest)) == manifest


def test_canonical_bytes_is_deterministic_sorted_and_compact():
    encoded = canonical_bytes(_manifest())
    assert encoded.endswith(b"\n")
    text = encoded.decode("utf-8")
    assert b": " not in encoded  # no spaces in separators
    assert text.index('"files"') < text.index('"version"')


def test_payload_file_as_dict_has_exact_keys():
    assert set(PayloadFile(**_file_dict()).as_dict()) == set(_file_dict())


def test_manifest_as_dict_has_exact_keys():
    assert set(_manifest().as_dict()) == set(_manifest_dict())


# ---------------------------------------------------------------------------
# payload_file_mode — os.name dispatch
# ---------------------------------------------------------------------------


def test_payload_file_mode_dispatches_on_os_name(monkeypatch):
    assert payload_file_mode() == 0o500  # posix default on the test host
    monkeypatch.setattr(os, "name", "nt")
    assert payload_file_mode() == 0o555


# ---------------------------------------------------------------------------
# invalid encoding (UnicodeDecodeError + JSONDecodeError arms)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoded",
    [
        b"\xff\xfe not valid utf8",  # UnicodeDecodeError
        b"{not json",  # JSONDecodeError
    ],
)
def test_rejects_invalid_encoding(encoded):
    with pytest.raises(ExecutionContractError, match="encoding is invalid"):
        parse_manifest(encoded)


# ---------------------------------------------------------------------------
# top-level schema
# ---------------------------------------------------------------------------


def test_rejects_non_object_root():
    with pytest.raises(ExecutionContractError, match="schema is invalid"):
        parse_manifest(_encode([]))  # type: ignore[arg-type]


def test_rejects_unknown_top_level_key():
    d = _manifest_dict()
    d["extra"] = 1
    with pytest.raises(ExecutionContractError, match="schema is invalid"):
        parse_manifest(_encode(d))


def test_rejects_missing_top_level_key():
    d = _manifest_dict()
    del d["run_id"]
    with pytest.raises(ExecutionContractError, match="schema is invalid"):
        parse_manifest(_encode(d))


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", [3, "2", 2.0])
def test_rejects_bad_version(version):
    with pytest.raises(ExecutionContractError, match="version is invalid"):
        parse_manifest(_encoded_with(version=version))


# ---------------------------------------------------------------------------
# run_directory
# ---------------------------------------------------------------------------


def test_rejects_non_object_run_directory():
    with pytest.raises(ExecutionContractError, match="run directory is invalid"):
        parse_manifest(_encoded_with(run_directory=7))


def test_rejects_bad_run_directory_keys():
    with pytest.raises(ExecutionContractError, match="run directory is invalid"):
        parse_manifest(_encoded_with(run_directory={"device": 7}))


# ---------------------------------------------------------------------------
# field-level validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": 5},                       # not str
        {"run_id": "bad/name"},              # not SAFE_NAME
        {"payload_root": 1},                 # not str
        {"payload_root": "other"},           # not in PAYLOAD_ROOTS
        {"payload_root_fingerprint": 1},     # not str
        {"payload_root_fingerprint": "xyz"},  # not SHA256
        {"files": 1},                        # not list
        {"files": []},                       # empty
    ],
)
def test_rejects_invalid_fields(overrides):
    with pytest.raises(ExecutionContractError, match="schema is invalid"):
        parse_manifest(_encoded_with(**overrides))


# ---------------------------------------------------------------------------
# per-file schema + field validation (_parse_file)
# ---------------------------------------------------------------------------


def test_rejects_non_object_file():
    with pytest.raises(ExecutionContractError, match="file schema is invalid"):
        parse_manifest(_encoded_with_files([1]))


def test_rejects_bad_file_keys():
    bad = _file_dict()
    del bad["size"]
    with pytest.raises(ExecutionContractError, match="file schema is invalid"):
        parse_manifest(_encoded_with_files([bad]))


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", 1),            # not str
        ("name", "bad/name"),   # not SAFE_NAME
        ("sha256", 1),          # not str
        ("sha256", "xyz"),      # not SHA256
        ("kind", "dir"),        # wrong kind
        ("mode", 0o644),        # wrong mode
    ],
)
def test_rejects_invalid_file_fields(field, value):
    bad = _file_dict()
    bad[field] = value
    with pytest.raises(ExecutionContractError, match="file is invalid"):
        parse_manifest(_encoded_with_files([bad]))


# ---------------------------------------------------------------------------
# _required_int (non-int and negative) — via file ints and run-directory ints
# ---------------------------------------------------------------------------


def test_rejects_non_integer_file_size():
    bad = _file_dict()
    bad["size"] = "5"
    with pytest.raises(ExecutionContractError, match="integer is invalid"):
        parse_manifest(_encoded_with_files([bad]))


def test_rejects_negative_file_inode():
    bad = _file_dict()
    bad["inode"] = -1
    with pytest.raises(ExecutionContractError, match="integer is invalid"):
        parse_manifest(_encoded_with_files([bad]))


def test_rejects_non_integer_run_directory_device():
    with pytest.raises(ExecutionContractError, match="integer is invalid"):
        parse_manifest(_encoded_with(run_directory={"device": "x", "inode": 8}))


def test_rejects_negative_run_directory_inode():
    with pytest.raises(ExecutionContractError, match="integer is invalid"):
        parse_manifest(_encoded_with(run_directory={"device": 7, "inode": -8}))


# ---------------------------------------------------------------------------
# canonical ordering, duplicate detection
# ---------------------------------------------------------------------------


def test_rejects_non_canonical_file_order():
    files = [_file_dict("b.tar"), _file_dict("a.tar")]  # unsorted
    with pytest.raises(ExecutionContractError, match="not canonical"):
        parse_manifest(_encoded_with_files(files))


def test_rejects_duplicate_file_names():
    files = [_file_dict("a.tar"), _file_dict("a.tar")]  # sorted, but dup
    with pytest.raises(ExecutionContractError, match="duplicate files"):
        parse_manifest(_encoded_with_files(files))


# ---------------------------------------------------------------------------
# strict object decoding (_strict_object) — duplicate JSON keys
# ---------------------------------------------------------------------------


def test_rejects_duplicate_json_keys():
    file_block = json.dumps([_file_dict()]).encode("utf-8")
    raw = (
        b'{"files":' + file_block + b',"payload_root":"claude-cli",'
        b'"payload_root_fingerprint":"' + (b"b" * 64) + b'",'
        b'"run_directory":{"device":7,"inode":8},'
        b'"run_id":"run-42","run_id":"run-43","version":2}\n'
    )
    with pytest.raises(ExecutionContractError, match="duplicate keys"):
        parse_manifest(raw)


# ---------------------------------------------------------------------------
# canonical-bytes mismatch (valid schema, non-canonical encoding)
# ---------------------------------------------------------------------------


def test_rejects_non_canonical_encoding():
    # Same logical content but spaced separators -> parses to a valid manifest
    # yet differs from the canonical byte form.
    loose = (
        json.dumps(_manifest_dict(), separators=(", ", ": ")) + "\n"
    ).encode("utf-8")
    with pytest.raises(ExecutionContractError, match="encoding is not canonical"):
        parse_manifest(loose)


def test_accepts_provider_cli_payload_root():
    d = _manifest_dict()
    d["payload_root"] = "provider-cli"
    assert parse_manifest(_encode(d)).payload_root == "provider-cli"

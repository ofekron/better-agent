"""100% unit coverage of provider_runtime_payload_codec.

The codec is the security boundary that turns an untrusted on-disk capability
artifact (manifest + payload bytes) into validated runtime state. Every
``raise ExecutionContractError`` is a distinct rejection a tampered or
malformed artifact must hit. This module pins each branch with a real
assertion that the malformed input is rejected, plus a happy-path round trip
that proves a well-formed artifact decodes.

Run with:
    cd backend && ./scripts/run-backend-tests.sh -- scripts/test_provider_runtime_payload_codec_unit.py
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-codec-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_manifest import artifact_family_kinds  # noqa: E402
from provider_runtime_capability_model import (  # noqa: E402
    CAPABILITY_MANIFEST_SCHEMA,
    CAPABILITY_PAYLOAD_NAME,
    CAPABILITY_PAYLOAD_SCHEMA,
    MAX_AGENTS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PAYLOAD_BYTES,
    MAX_SKILLS,
    normalize_plan,
    normalize_prewarm_status,
    semantic_fingerprint,
)
import provider_runtime_payload_codec as codec  # noqa: E402
from provider_runtime_payload_codec import (  # noqa: E402
    decode_runtime_capability_payload,
    validate_runtime_capability_manifest,
)

_NON_CLAUDE_FAMILY = next(k for k in artifact_family_kinds() if k != "claude")
_HEX64 = "0" * 64


def _identity() -> dict:
    return {
        "requested_path": "/abs/source",
        "resolved_path": "/abs/source",
        "sha256": _HEX64,
        "size": 0,
        "mtime_ns": 0,
        "ctime_ns": 0,
        "device": 0,
        "inode": 0,
        "symlink_chain": [["/abs/source", "/abs/source"]],
    }


def _skill_file(owner: str = "planning", path: str = "planning/SKILL.md") -> dict:
    contents = b"skill body"
    return {
        "kind": "skill",
        "owner": owner,
        "path": path,
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size": len(contents),
        "mode": 0o400,
        "source_identity": _identity(),
        "contents": base64.b64encode(contents).decode("ascii"),
    }


def _package_fingerprint(identities: list) -> str:
    return hashlib.sha256(
        json.dumps(identities, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()


def _make_plan() -> dict:
    return normalize_plan(
        {
            "harness": {"instructions": ["runtime"], "tool_policy": {"allow": ["Read"]}},
            "tools": ["Read"],
            "mcp_servers": [
                {
                    "name": "scheduler",
                    "transport": "stdio",
                    "config": {"argv": ["/bin/sched"]},
                    "tool_names": ["Read"],
                    "prewarm": {"eligible": True, "readiness_required": False},
                },
            ],
        },
    )


def _make_valid(*, family: str = "claude") -> tuple[dict, bytes, dict]:
    """Build a fully consistent (manifest, payload_bytes, payload_dict)."""
    plan = _make_plan()
    prewarm_status = normalize_prewarm_status(plan, {})
    file_entry = _skill_file()
    payload_dict = {
        "schema": CAPABILITY_PAYLOAD_SCHEMA,
        "family": family,
        "plan": plan,
        "extension_state": {},
        "installation_decisions": {"mode": "default"},
        "package_identities": [],
        "prewarm_status": prewarm_status,
        "files": [file_entry],
    }
    payload_bytes = json.dumps(
        payload_dict, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    manifest = {
        "schema": CAPABILITY_MANIFEST_SCHEMA,
        "family": family,
        "path": CAPABILITY_PAYLOAD_NAME,
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "size": len(payload_bytes),
        "file_count": 1,
        "skill_count": 1,
        "agent_count": 0,
        "extension_ids": [],
        "tool_names": list(plan["tools"]),
        "semantic_fingerprint": semantic_fingerprint(plan),
        "package_fingerprint": _package_fingerprint([]),
        "prewarm_status": prewarm_status,
    }
    return manifest, payload_bytes, payload_dict


def _expect_error(fn, *args, **kwargs):
    with pytest.raises(ExecutionContractError):
        fn(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


def test_manifest_valid_returns_normalized_copy() -> None:
    manifest, _, _ = _make_valid()
    result = validate_runtime_capability_manifest(manifest)
    assert result == json.loads(json.dumps(manifest, sort_keys=True))
    # Returned object is a fresh copy, not the input identity.
    assert result is not manifest


def test_decode_valid_round_trips() -> None:
    manifest, payload_bytes, payload_dict = _make_valid()
    decoded, files = decode_runtime_capability_payload(payload_bytes, manifest)
    assert decoded["family"] == "claude"
    assert decoded["extension_state"] == {}
    assert decoded["installation_decisions"] == {"mode": "default"}
    assert decoded["package_identities"] == []
    assert len(files) == 1
    metadata, contents = files[0]
    assert metadata["kind"] == "skill"
    assert contents == b"skill body"
    # plan + prewarm_status are normalized in place inside the decoded dict.
    assert decoded["plan"]["tools"] == manifest["tool_names"]


def test_decode_non_claude_family_with_zero_agents_ok() -> None:
    manifest, payload_bytes, _ = _make_valid(family=_NON_CLAUDE_FAMILY)
    decoded, _files = decode_runtime_capability_payload(payload_bytes, manifest)
    assert decoded["family"] == _NON_CLAUDE_FAMILY


# --------------------------------------------------------------------------- #
# validate_runtime_capability_manifest rejection branches
# --------------------------------------------------------------------------- #


def _mexpect(mutator) -> None:
    manifest, _, _ = _make_valid()
    mutator(manifest)
    _expect_error(validate_runtime_capability_manifest, manifest)


def test_manifest_rejects_non_dict() -> None:
    _expect_error(validate_runtime_capability_manifest, ["not", "a", "dict"])


def test_manifest_rejects_missing_key() -> None:
    def m(mn):
        del mn["semantic_fingerprint"]
    _mexpect(m)


def test_manifest_rejects_extra_key() -> None:
    def m(mn):
        mn["extra"] = 1
    _mexpect(m)


def test_manifest_rejects_wrong_schema() -> None:
    def m(mn):
        mn["schema"] = 999
    _mexpect(m)


def test_manifest_rejects_unknown_family() -> None:
    def m(mn):
        mn["family"] = "nope"
    _mexpect(m)


def test_manifest_rejects_wrong_path() -> None:
    def m(mn):
        mn["path"] = "other.json"
    _mexpect(m)


def test_manifest_rejects_sha_not_str() -> None:
    def m(mn):
        mn["sha256"] = 5
    _mexpect(m)


def test_manifest_rejects_sha_bad_format() -> None:
    def m(mn):
        mn["sha256"] = "zz"
    _mexpect(m)


def test_manifest_rejects_size_not_int() -> None:
    def m(mn):
        mn["size"] = "1"
    _mexpect(m)


def test_manifest_rejects_size_zero() -> None:
    def m(mn):
        mn["size"] = 0
    _mexpect(m)


def test_manifest_rejects_size_over_max() -> None:
    def m(mn):
        mn["size"] = MAX_PAYLOAD_BYTES + 1
    _mexpect(m)


def test_manifest_rejects_file_count_not_int() -> None:
    def m(mn):
        mn["file_count"] = 1.0
    _mexpect(m)


def test_manifest_rejects_file_count_over_max() -> None:
    def m(mn):
        mn["file_count"] = MAX_FILES + 1
    _mexpect(m)


def test_manifest_rejects_skill_count_over_max() -> None:
    def m(mn):
        mn["skill_count"] = MAX_SKILLS + 1
    _mexpect(m)


def test_manifest_rejects_agent_count_over_max() -> None:
    def m(mn):
        mn["agent_count"] = MAX_AGENTS + 1
    _mexpect(m)


def test_manifest_rejects_non_claude_with_agents() -> None:
    def m(mn):
        mn["family"] = _NON_CLAUDE_FAMILY
        mn["agent_count"] = 1
    _mexpect(m)


def test_manifest_rejects_extension_ids_not_list() -> None:
    def m(mn):
        mn["extension_ids"] = "ext"
    _mexpect(m)


def test_manifest_rejects_extension_ids_non_str_entry() -> None:
    def m(mn):
        mn["extension_ids"] = [1]
    _mexpect(m)


def test_manifest_rejects_tool_names_not_list() -> None:
    def m(mn):
        mn["tool_names"] = "Read"
    _mexpect(m)


def test_manifest_rejects_tool_names_non_str_entry() -> None:
    def m(mn):
        mn["tool_names"] = [1]
    _mexpect(m)


def test_manifest_rejects_semantic_fingerprint_bad_format() -> None:
    def m(mn):
        mn["semantic_fingerprint"] = "no"
    _mexpect(m)


def test_manifest_rejects_package_fingerprint_bad_format() -> None:
    def m(mn):
        mn["package_fingerprint"] = "no"
    _mexpect(m)


def test_manifest_rejects_prewarm_not_dict() -> None:
    def m(mn):
        mn["prewarm_status"] = []
    _mexpect(m)


def test_manifest_rejects_extension_ids_not_sorted() -> None:
    def m(mn):
        mn["extension_ids"] = ["b", "a"]
    _mexpect(m)


def test_manifest_rejects_tool_names_duplicate() -> None:
    def m(mn):
        mn["tool_names"] = ["Read", "Read"]
    _mexpect(m)


def test_manifest_rejects_secret_in_field() -> None:
    """frozen_manifest_json secret scan must reject a leaked secret value."""
    def m(mn):
        mn["path"] = "token=abc"
    _mexpect(m)


# --------------------------------------------------------------------------- #
# decode_runtime_capability_payload top-level rejection branches
# --------------------------------------------------------------------------- #


def _dexpect(payload_mutator, *, manifest_mutator=None) -> None:
    manifest, _payload_bytes, payload_dict = _make_valid()
    payload_mutator(payload_dict)
    payload_bytes = json.dumps(
        payload_dict, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    # Keep manifest size/sha consistent with the re-serialized payload so the
    # top-level size/sha gate passes and the targeted branch is reached.
    manifest = copy.deepcopy(manifest)
    manifest["sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    manifest["size"] = len(payload_bytes)
    if manifest_mutator is not None:
        manifest_mutator(manifest)
    _expect_error(decode_runtime_capability_payload, payload_bytes, manifest)


def test_decode_rejects_payload_size_mismatch() -> None:
    manifest, payload_bytes, _ = _make_valid()
    _expect_error(decode_runtime_capability_payload, payload_bytes + b"x", manifest)


def test_decode_rejects_payload_sha_mismatch() -> None:
    manifest, payload_bytes, _ = _make_valid()
    manifest = copy.deepcopy(manifest)
    manifest["sha256"] = "f" * 64
    _expect_error(decode_runtime_capability_payload, payload_bytes, manifest)


def test_decode_rejects_invalid_json() -> None:
    # Garbage must clear the size+sha gate (manifest updated to match) so the
    # failure lands on json.loads, not the earlier payload-size check.
    manifest, _payload_bytes, _ = _make_valid()
    garbage = b"\x00\x01\x02 not json"
    manifest = copy.deepcopy(manifest)
    manifest["sha256"] = hashlib.sha256(garbage).hexdigest()
    manifest["size"] = len(garbage)
    _expect_error(decode_runtime_capability_payload, garbage, manifest)


def test_decode_rejects_payload_non_dict() -> None:
    _dexpect(lambda p: p.clear())


def test_decode_rejects_payload_missing_key() -> None:
    _dexpect(lambda p: p.pop("files"))


def test_decode_rejects_payload_extra_key() -> None:
    _dexpect(lambda p: p.__setitem__("extra", 1))


def test_decode_rejects_payload_wrong_schema() -> None:
    _dexpect(lambda p: p.__setitem__("schema", 999))


def test_decode_rejects_payload_family_mismatch() -> None:
    _dexpect(lambda p: p.__setitem__("family", _NON_CLAUDE_FAMILY))


def test_decode_rejects_extension_state_not_dict() -> None:
    _dexpect(lambda p: p.__setitem__("extension_state", []))


def test_decode_rejects_extension_state_ids_mismatch() -> None:
    def m(mn):
        mn["extension_ids"] = ["ext"]
    _dexpect(lambda p: None, manifest_mutator=m)


def test_decode_rejects_installation_decisions_not_dict() -> None:
    _dexpect(lambda p: p.__setitem__("installation_decisions", []))


def test_decode_rejects_package_identities_not_list() -> None:
    _dexpect(lambda p: p.__setitem__("package_identities", {}))


def test_decode_rejects_prewarm_status_not_dict() -> None:
    _dexpect(lambda p: p.__setitem__("prewarm_status", []))


def test_decode_rejects_files_not_list() -> None:
    _dexpect(lambda p: p.__setitem__("files", {}))


def test_decode_rejects_file_count_mismatch() -> None:
    _dexpect(lambda p: p["files"].append(copy.deepcopy(p["files"][0])))


def test_decode_rejects_plan_tools_mismatch() -> None:
    def mut(p):
        plan = copy.deepcopy(p["plan"])
        plan["tools"] = ["Write"]
        p["plan"] = plan
    _dexpect(mut)


def test_decode_rejects_semantic_fingerprint_mismatch() -> None:
    def m(mn):
        mn["semantic_fingerprint"] = "a" * 64
    _dexpect(lambda p: None, manifest_mutator=m)


def test_decode_rejects_package_identity_non_dict_entry() -> None:
    _dexpect(lambda p: p.__setitem__("package_identities", ["not-dict"]))


def test_decode_rejects_package_fingerprint_mismatch() -> None:
    def mut(p):
        p["package_identities"] = []
    def m(mn):
        mn["package_fingerprint"] = "a" * 64
    _dexpect(mut, manifest_mutator=m)


def test_decode_rejects_prewarm_status_mismatch() -> None:
    def m(mn):
        mn["prewarm_status"] = {}
    _dexpect(lambda p: None, manifest_mutator=m)


def test_decode_rejects_skill_count_mismatch() -> None:
    def m(mn):
        mn["skill_count"] = 2
    _dexpect(lambda p: None, manifest_mutator=m)


def test_decode_rejects_agent_count_mismatch() -> None:
    def m(mn):
        mn["agent_count"] = 1
    _dexpect(lambda p: None, manifest_mutator=m)


def test_decode_rejects_duplicate_file_targets() -> None:
    def mut(p):
        # Two skill files collapsing to the same (kind, owner, path) target.
        dup = copy.deepcopy(p["files"][0])
        p["files"].append(dup)

    def m(mn):
        mn["file_count"] = 2
    _dexpect(mut, manifest_mutator=m)


def test_decode_rejects_total_file_bytes_exceeded(monkeypatch) -> None:
    # MAX_TOTAL_FILE_BYTES (32 MiB) is impractical to exceed with real bytes;
    # lower the cap on the codec module to exercise the real boundary cheaply.
    monkeypatch.setattr(codec, "MAX_TOTAL_FILE_BYTES", 0)
    _expect_error(decode_runtime_capability_payload, *_make_valid()[:2])


# --------------------------------------------------------------------------- #
# _decoded_file rejection branches (reached through decode)
# --------------------------------------------------------------------------- #


def _fexpect(file_mutator) -> None:
    """Mutate the single file entry and assert decode rejects it."""
    def mut(p):
        file_mutator(p["files"][0])
    _dexpect(mut)


def test_file_rejects_wrong_key_set() -> None:
    _fexpect(lambda f: f.pop("mode"))


def test_file_rejects_bad_kind() -> None:
    _fexpect(lambda f: f.__setitem__("kind", "plugin"))


def test_file_rejects_bad_owner() -> None:
    _fexpect(lambda f: f.__setitem__("owner", "bad owner!"))


def test_file_rejects_empty_path() -> None:
    _fexpect(lambda f: f.__setitem__("path", ""))


def test_file_rejects_absolute_path() -> None:
    _fexpect(lambda f: f.__setitem__("path", "/abs/path"))


def test_file_rejects_traversal_path() -> None:
    _fexpect(lambda f: f.__setitem__("path", "a/../b"))


def test_file_rejects_path_too_deep() -> None:
    _fexpect(lambda f: f.__setitem__("path", "/".join(f"a{i}" for i in range(33))))


def test_file_rejects_bad_sha_format() -> None:
    _fexpect(lambda f: f.__setitem__("sha256", "zz"))


def test_file_rejects_size_over_max() -> None:
    _fexpect(lambda f: f.__setitem__("size", MAX_FILE_BYTES + 1))


def test_file_rejects_bad_mode() -> None:
    _fexpect(lambda f: f.__setitem__("mode", 0o600))


def test_file_rejects_source_identity_not_dict() -> None:
    _fexpect(lambda f: f.__setitem__("source_identity", []))


def test_file_rejects_contents_not_str() -> None:
    _fexpect(lambda f: f.__setitem__("contents", 1))


def test_file_rejects_invalid_base64() -> None:
    _fexpect(lambda f: f.__setitem__("contents", "@@@@"))


def test_file_rejects_size_contents_mismatch() -> None:
    _fexpect(lambda f: f.__setitem__("size", 999))


def test_file_rejects_sha_contents_mismatch() -> None:
    _fexpect(lambda f: f.__setitem__("sha256", "b" * 64))


def test_file_rejects_bad_source_identity() -> None:
    _fexpect(lambda f: f.__setitem__("source_identity", {"wrong": "shape"}))


def test_agent_file_rejects_path_owner_mismatch() -> None:
    """Agent kind requires path == owner and owner be a bare name."""
    manifest, _payload_bytes, payload_dict = _make_valid()
    contents = b"agent body"
    payload_dict["files"] = [
        {
            "kind": "agent",
            "owner": "reviewer",
            "path": "reviewer/extra.md",
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size": len(contents),
            "mode": 0o400,
            "source_identity": _identity(),
            "contents": base64.b64encode(contents).decode("ascii"),
        },
    ]
    manifest = copy.deepcopy(manifest)
    manifest["agent_count"] = 1
    manifest["skill_count"] = 0
    payload_bytes = json.dumps(
        payload_dict, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    manifest["sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    manifest["size"] = len(payload_bytes)
    _expect_error(decode_runtime_capability_payload, payload_bytes, manifest)

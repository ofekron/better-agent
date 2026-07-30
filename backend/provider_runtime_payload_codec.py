from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError, SHA256_RE
from codex_execution_identity import file_identity_from_dict
from provider_family_launch_attestation import CriticalPackageIdentity
from provider_manifest import artifact_family_kinds
from provider_runtime_capability_model import (
    CAPABILITY_MANIFEST_SCHEMA,
    CAPABILITY_PAYLOAD_NAME,
    CAPABILITY_PAYLOAD_SCHEMA,
    MAX_AGENTS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PAYLOAD_BYTES,
    MAX_SKILLS,
    MAX_TOTAL_FILE_BYTES,
    frozen_json,
    frozen_manifest_json,
    normalize_plan,
    semantic_fingerprint,
    validate_frozen_prewarm_status,
)


_SAFE_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")


def validate_runtime_capability_manifest(
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {
        "schema",
        "family",
        "path",
        "sha256",
        "size",
        "file_count",
        "skill_count",
        "agent_count",
        "extension_ids",
        "tool_names",
        "semantic_fingerprint",
        "package_fingerprint",
        "prewarm_status",
    }
    if type(raw) is not dict or set(raw) != expected:
        raise ExecutionContractError("invalid runtime capability manifest")
    if (
        raw["schema"] != CAPABILITY_MANIFEST_SCHEMA
        or raw["family"] not in artifact_family_kinds()
        or raw["path"] != CAPABILITY_PAYLOAD_NAME
        or type(raw["sha256"]) is not str
        or not SHA256_RE.fullmatch(raw["sha256"])
        or type(raw["size"]) is not int
        or not 0 < raw["size"] <= MAX_PAYLOAD_BYTES
        or type(raw["file_count"]) is not int
        or not 0 <= raw["file_count"] <= MAX_FILES
        or type(raw["skill_count"]) is not int
        or not 0 <= raw["skill_count"] <= MAX_SKILLS
        or type(raw["agent_count"]) is not int
        or not 0 <= raw["agent_count"] <= MAX_AGENTS
        or (
            raw["family"] != "claude"
            and raw["agent_count"] != 0
        )
        or type(raw["extension_ids"]) is not list
        or any(type(value) is not str for value in raw["extension_ids"])
        or type(raw["tool_names"]) is not list
        or any(type(value) is not str for value in raw["tool_names"])
        or type(raw["semantic_fingerprint"]) is not str
        or not SHA256_RE.fullmatch(raw["semantic_fingerprint"])
        or type(raw["package_fingerprint"]) is not str
        or not SHA256_RE.fullmatch(raw["package_fingerprint"])
        or type(raw["prewarm_status"]) is not dict
    ):
        raise ExecutionContractError("invalid runtime capability manifest")
    if (
        raw["extension_ids"] != sorted(set(raw["extension_ids"]))
        or len(set(raw["tool_names"])) != len(raw["tool_names"])
    ):
        raise ExecutionContractError("invalid runtime capability manifest")
    frozen_manifest_json(raw, label="runtime capability manifest")
    return json.loads(json.dumps(raw, sort_keys=True))


def _decoded_file(raw: Any) -> tuple[dict[str, Any], bytes]:
    if type(raw) is not dict or set(raw) != {
        "kind",
        "owner",
        "path",
        "sha256",
        "size",
        "mode",
        "source_identity",
        "contents",
    }:
        raise ExecutionContractError("invalid runtime capability file")
    path = Path(raw["path"]) if type(raw["path"]) is str else Path("..")
    if (
        raw["kind"] not in {"agent", "skill"}
        or type(raw["owner"]) is not str
        or not _SAFE_OWNER_RE.fullmatch(raw["owner"])
        or not raw["path"]
        or path.is_absolute()
        or ".." in path.parts
        or len(path.parts) > 32
        or type(raw["sha256"]) is not str
        or not SHA256_RE.fullmatch(raw["sha256"])
        or type(raw["size"]) is not int
        or not 0 <= raw["size"] <= MAX_FILE_BYTES
        or raw["mode"] not in {0o400, 0o500}
        or type(raw["source_identity"]) is not dict
        or type(raw["contents"]) is not str
        or (
            raw["kind"] == "agent"
            and (
                raw["path"] != raw["owner"]
                or Path(raw["owner"]).name != raw["owner"]
            )
        )
    ):
        raise ExecutionContractError("invalid runtime capability file")
    file_identity_from_dict(raw["source_identity"])
    try:
        contents = base64.b64decode(raw["contents"], validate=True)
    except ValueError as exc:
        raise ExecutionContractError(
            "invalid runtime capability file",
        ) from exc
    if (
        len(contents) != raw["size"]
        or hashlib.sha256(contents).hexdigest() != raw["sha256"]
    ):
        raise ExecutionContractError("runtime capability file mismatch")
    return dict(raw), contents


def decode_runtime_capability_payload(
    payload: bytes,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[tuple[dict[str, Any], bytes], ...]]:
    expected = validate_runtime_capability_manifest(manifest)
    if (
        len(payload) != expected["size"]
        or hashlib.sha256(payload).hexdigest() != expected["sha256"]
    ):
        raise ExecutionContractError("runtime capability payload mismatch")
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(
            "invalid runtime capability payload",
        ) from exc
    if type(raw) is not dict or set(raw) != {
        "schema",
        "family",
        "plan",
        "extension_state",
        "installation_decisions",
        "package_identities",
        "prewarm_status",
        "files",
    }:
        raise ExecutionContractError("invalid runtime capability payload")
    plan = normalize_plan(raw["plan"])
    prewarm_status = validate_frozen_prewarm_status(
        plan,
        raw["prewarm_status"],
    )
    if (
        raw["schema"] != CAPABILITY_PAYLOAD_SCHEMA
        or raw["family"] != expected["family"]
        or type(raw["extension_state"]) is not dict
        or sorted(raw["extension_state"]) != expected["extension_ids"]
        or type(raw["installation_decisions"]) is not dict
        or type(raw["package_identities"]) is not list
        or type(raw["prewarm_status"]) is not dict
        or prewarm_status != expected["prewarm_status"]
        or type(raw["files"]) is not list
        or len(raw["files"]) != expected["file_count"]
        or plan["tools"] != expected["tool_names"]
        or semantic_fingerprint(plan) != expected["semantic_fingerprint"]
    ):
        raise ExecutionContractError("runtime capability payload mismatch")
    frozen_json(
        raw["extension_state"],
        label="extension capability state",
    )
    frozen_json(
        raw["installation_decisions"],
        label="installation profile decisions",
    )
    packages = [
        CriticalPackageIdentity.from_dict(value)
        if type(value) is dict
        else None
        for value in raw["package_identities"]
    ]
    if any(value is None for value in packages) or (
        hashlib.sha256(
            json.dumps(
                raw["package_identities"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        != expected["package_fingerprint"]
    ):
        raise ExecutionContractError("runtime package identity mismatch")
    files = tuple(_decoded_file(value) for value in raw["files"])
    targets = {
        (metadata["kind"], metadata["owner"], metadata["path"])
        for metadata, _contents in files
    }
    skill_names = {
        metadata["owner"]
        for metadata, _contents in files
        if metadata["kind"] == "skill"
    }
    agent_names = {
        metadata["owner"]
        for metadata, _contents in files
        if metadata["kind"] == "agent"
    }
    if (
        len(skill_names) != expected["skill_count"]
        or len(agent_names) != expected["agent_count"]
        or len(targets) != len(files)
        or sum(len(contents) for _metadata, contents in files)
        > MAX_TOTAL_FILE_BYTES
    ):
        raise ExecutionContractError("runtime capability count mismatch")
    raw["plan"] = plan
    raw["prewarm_status"] = prewarm_status
    return raw, files

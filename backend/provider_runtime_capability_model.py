from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError, canonical_json
from harness_secret_refs import (
    HarnessSecretRefError,
    normalize_harness_secret_refs,
)
from provider_manifest import artifact_family_kinds


CAPABILITY_PAYLOAD_SCHEMA = 1
CAPABILITY_MANIFEST_SCHEMA = 1
CAPABILITY_PAYLOAD_NAME = "family-runtime-capabilities.json"
CAPABILITY_FILES_DIR = "runtime-capabilities"
MAX_PAYLOAD_BYTES = 48 * 1024 * 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 32 * 1024 * 1024
MAX_SKILLS = 128
MAX_AGENTS = 128
_FAMILIES = artifact_family_kinds()
_TRANSPORTS = frozenset({"http", "sdk", "sse", "stdio"})
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|auth|authorization|credential|password|secret|token)($|_)",
)
_SECRET_ERROR_RE = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|credential|password|secret|token)\b",
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(^|[-_])(api[-_]?key|authorization|auth|credential|password|secret|token)"
    r"($|=)",
)


def _json_encode(value: Any, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError(f"{label} must be JSON-compatible") from exc


def frozen_json(value: Any, *, label: str) -> str:
    reject_secrets(value, label=label)
    return _json_encode(value, label=label)


def reject_secrets(
    value: Any,
    *,
    label: str,
    depth: int = 0,
    allow_reference_keys: bool = True,
) -> None:
    if depth > 32:
        raise ExecutionContractError(f"{label} is nested too deeply")
    if type(value) is str:
        if _SECRET_VALUE_RE.search(value):
            raise ExecutionContractError(f"{label} must be secret-free")
        return
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ExecutionContractError(f"{label} is invalid")
        return
    if type(value) is list:
        if any(
            type(item) is str and _SECRET_VALUE_RE.search(item)
            for item in value
        ):
            raise ExecutionContractError(f"{label} must be secret-free")
        for item in value:
            reject_secrets(
                item,
                label=label,
                depth=depth + 1,
                allow_reference_keys=allow_reference_keys,
            )
        return
    if type(value) is not dict:
        raise ExecutionContractError(f"{label} must be JSON-compatible")
    for key, item in value.items():
        if type(key) is not str:
            raise ExecutionContractError(f"{label} keys must be strings")
        normalized = key.lower().replace("-", "_")
        if (
            _SECRET_KEY_RE.search(normalized)
            and (
                not allow_reference_keys
                or not normalized.endswith(("_ref", "_refs"))
            )
            and item not in (None, "", [], {})
        ):
            raise ExecutionContractError(f"{label} must be secret-free")
        reject_secrets(
            item,
            label=label,
            depth=depth + 1,
            allow_reference_keys=allow_reference_keys,
        )


def _string_list(value: Any, *, label: str) -> list[str]:
    if (
        type(value) is not list
        or any(
            type(item) is not str
            or not item
            or len(item) > 512
            or "\x00" in item
            or "\n" in item
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise ExecutionContractError(f"invalid {label}")
    return list(value)


def normalize_harness_plan(raw: Mapping[str, Any]) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ExecutionContractError("invalid harness plan")
    harness = json.loads(
        _json_encode(raw, label="harness plan"),
    )
    launcher = harness.get("launcher_projection")
    top_present = "secret_refs" in harness
    launcher_present = (
        type(launcher) is dict
        and "secret_refs" in launcher
    )
    if top_present != launcher_present:
        raise ExecutionContractError("harness secret_refs authority mismatch")
    refs: dict[str, list[str]] | None = None
    if top_present:
        try:
            refs = normalize_harness_secret_refs(harness["secret_refs"])
            launcher_refs = normalize_harness_secret_refs(
                launcher["secret_refs"],
            )
        except HarnessSecretRefError as exc:
            raise ExecutionContractError(str(exc)) from exc
        if refs != launcher_refs:
            raise ExecutionContractError(
                "harness secret_refs authority mismatch",
            )
        harness.pop("secret_refs")
        launcher = dict(launcher)
        launcher.pop("secret_refs")
        harness["launcher_projection"] = launcher
    reject_secrets(
        harness,
        label="harness plan",
        allow_reference_keys=False,
    )
    normalized = json.loads(
        _json_encode(harness, label="harness plan"),
    )
    if refs is not None:
        normalized["secret_refs"] = refs
        normalized_launcher = dict(normalized["launcher_projection"])
        normalized_launcher["secret_refs"] = refs
        normalized["launcher_projection"] = normalized_launcher
    return normalized


def normalize_plan(raw: Mapping[str, Any]) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != {
        "harness",
        "tools",
        "mcp_servers",
    }:
        raise ExecutionContractError("invalid frozen capability plan")
    if type(raw["harness"]) is not dict or type(raw["mcp_servers"]) is not list:
        raise ExecutionContractError("invalid frozen capability plan")
    harness = normalize_harness_plan(raw["harness"])
    tools = _string_list(raw["tools"], label="tool plan")
    servers: list[dict[str, Any]] = []
    names: set[str] = set()
    for server in raw["mcp_servers"]:
        if type(server) is not dict or set(server) != {
            "name",
            "transport",
            "config",
            "tool_names",
            "prewarm",
        }:
            raise ExecutionContractError("invalid frozen MCP plan")
        name = server["name"]
        prewarm = server["prewarm"]
        if (
            type(name) is not str
            or not _SAFE_NAME_RE.fullmatch(name)
            or name in names
            or server["transport"] not in _TRANSPORTS
            or type(server["config"]) is not dict
            or type(prewarm) is not dict
            or set(prewarm) != {"eligible", "readiness_required"}
            or type(prewarm["eligible"]) is not bool
            or type(prewarm["readiness_required"]) is not bool
            or (
                prewarm["readiness_required"]
                and not prewarm["eligible"]
            )
        ):
            raise ExecutionContractError("invalid frozen MCP plan")
        names.add(name)
        config = json.loads(
            frozen_json(server["config"], label="MCP configuration"),
        )
        tool_names = _string_list(
            server["tool_names"],
            label="MCP tool names",
        )
        servers.append({
            "name": name,
            "transport": server["transport"],
            "config": config,
            "tool_names": tool_names,
            "prewarm": dict(prewarm),
        })
    if any(
        tool not in tools
        for server in servers
        for tool in server["tool_names"]
    ):
        raise ExecutionContractError("MCP tool is absent from frozen tool plan")
    return {
        "harness": harness,
        "tools": tools,
        "mcp_servers": servers,
    }


def serialize_runtime_plan(plan: Mapping[str, Any]) -> str:
    return _json_encode(
        normalize_plan(plan),
        label="runtime capability plan",
    )


def normalize_prewarm_status(
    plan: Mapping[str, Any],
    raw_results: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if type(raw_results) is not dict:
        raise ExecutionContractError("invalid MCP prewarm results")
    servers = {
        server["name"]: server for server in plan["mcp_servers"]
    }
    if set(raw_results) - set(servers):
        raise ExecutionContractError("unknown MCP prewarm result")
    status: dict[str, dict[str, Any]] = {}
    for name, server in servers.items():
        policy = server["prewarm"]
        result = raw_results.get(name)
        if not policy["eligible"]:
            if result is not None:
                raise ExecutionContractError(
                    "ineligible MCP server has prewarm result",
                )
            status[name] = {
                "status": "not_eligible",
                "error": None,
                "launch_mode": "normal",
            }
            continue
        if result is None:
            if policy["readiness_required"]:
                raise ExecutionContractError(
                    "required MCP prewarm readiness is unavailable",
                )
            status[name] = {
                "status": "not_attempted",
                "error": None,
                "launch_mode": "normal",
            }
            continue
        if (
            type(result) is not dict
            or set(result) != {"status", "error"}
            or result["status"] not in {"failed", "ready"}
            or (
                result["status"] == "ready"
                and result["error"] is not None
            )
            or (
                result["status"] == "failed"
                and (
                    type(result["error"]) is not str
                    or not result["error"]
                    or len(result["error"]) > 512
                    or _SECRET_ERROR_RE.search(result["error"])
                )
            )
        ):
            raise ExecutionContractError("invalid MCP prewarm result")
        if result["status"] == "failed" and policy["readiness_required"]:
            raise ExecutionContractError(
                f"required MCP prewarm failed: {name}",
            )
        status[name] = {
            "status": result["status"],
            "error": result["error"],
            "launch_mode": (
                "prewarmed" if result["status"] == "ready" else "normal"
            ),
        }
    return status


def validate_frozen_prewarm_status(
    plan: Mapping[str, Any],
    raw_status: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    servers = {
        server["name"]: server for server in plan["mcp_servers"]
    }
    if type(raw_status) is not dict or set(raw_status) != set(servers):
        raise ExecutionContractError("invalid frozen MCP prewarm status")
    normalized: dict[str, dict[str, Any]] = {}
    for name, value in raw_status.items():
        policy = servers[name]["prewarm"]
        if (
            type(value) is not dict
            or set(value) != {"status", "error", "launch_mode"}
            or value["status"] not in {
                "failed",
                "not_attempted",
                "not_eligible",
                "ready",
            }
            or value["launch_mode"] not in {"normal", "prewarmed"}
            or (
                value["status"] == "ready"
                and (
                    value["error"] is not None
                    or value["launch_mode"] != "prewarmed"
                    or not policy["eligible"]
                )
            )
            or (
                value["status"] != "ready"
                and value["launch_mode"] != "normal"
            )
            or (
                value["status"] == "failed"
                and (
                    type(value["error"]) is not str
                    or not value["error"]
                    or len(value["error"]) > 512
                    or _SECRET_ERROR_RE.search(value["error"])
                    or policy["readiness_required"]
                )
            )
            or (
                value["status"] != "failed"
                and value["error"] is not None
            )
            or (
                value["status"] == "not_eligible"
                and policy["eligible"]
            )
            or (
                value["status"] == "not_attempted"
                and (
                    not policy["eligible"]
                    or policy["readiness_required"]
                )
            )
        ):
            raise ExecutionContractError("invalid frozen MCP prewarm status")
        normalized[name] = dict(value)
    return normalized


def semantic_fingerprint(plan: Mapping[str, Any]) -> str:
    semantic = {
        "harness": plan["harness"],
        "tools": plan["tools"],
        "mcp_servers": [
            {
                "name": server["name"],
                "transport": server["transport"],
                "config": server["config"],
                "tool_names": server["tool_names"],
            }
            for server in plan["mcp_servers"]
        ],
    }
    return hashlib.sha256(canonical_json(semantic)).hexdigest()


def mcp_configs(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        server["name"]: {
            "transport": server["transport"],
            "config": json.loads(
                json.dumps(server["config"], sort_keys=True),
            ),
            "tool_names": list(server["tool_names"]),
        }
        for server in plan["mcp_servers"]
    }


@dataclass(frozen=True)
class PreparedRuntimeCapabilities:
    _manifest_json: str = field(repr=False)
    payload: bytes = field(repr=False)
    _plan_json: str = field(repr=False)
    _prewarm_json: str = field(repr=False)

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def plan(self) -> dict[str, Any]:
        return json.loads(self._plan_json)

    @property
    def prewarm_status(self) -> dict[str, dict[str, Any]]:
        return json.loads(self._prewarm_json)

    @property
    def semantic_fingerprint(self) -> str:
        return semantic_fingerprint(self.plan)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self.plan["tools"])

    @property
    def mcp_configs(self) -> dict[str, dict[str, Any]]:
        return mcp_configs(self.plan)

    @classmethod
    def create(
        cls,
        *,
        manifest: Mapping[str, Any],
        payload: bytes,
        plan: Mapping[str, Any],
        prewarm_status: Mapping[str, Any],
    ) -> PreparedRuntimeCapabilities:
        return cls(
            frozen_json(manifest, label="runtime capability manifest"),
            bytes(payload),
            serialize_runtime_plan(plan),
            frozen_json(prewarm_status, label="MCP prewarm status"),
        )


@dataclass(frozen=True)
class RunLocalCapabilities:
    plan: dict[str, Any]
    extension_state: dict[str, Any]
    installation_decisions: dict[str, Any]
    skill_dirs: dict[str, Path]
    agent_files: dict[str, Path]
    prewarm_status: dict[str, dict[str, Any]]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self.plan["tools"])

    @property
    def mcp_configs(self) -> dict[str, dict[str, Any]]:
        return mcp_configs(self.plan)

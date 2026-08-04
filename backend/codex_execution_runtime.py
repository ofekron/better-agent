from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from codex_execution_contract import (
    CodexExecutionContract,
    codex_authority_paths,
)
from codex_execution_common import (
    ExecutionContractError,
    binary_open_flags,
    sha256_fd,
)
from codex_execution_identity import FileIdentity
from execution_artifact_io import (
    load_execution_artifact,
    validate_execution_input_projection,
)
from execution_template import (
    ExecutionArtifact,
    ExecutionAuthorityError,
    PreparedExecution,
    prepare_execution,
)
from provider_runner_launch import RunnerLaunch, retarget_runner_launch


_RUNTIME_POLICY_KEYS = {
    "context_strategy",
    "disabled_runtime_skills",
    "permission",
    "request_user_input_enabled",
    "run_policy",
    "runtime_agent_manifest",
    "worker_working_mode",
    "working_mode",
    "model_admission",
    "runner_launch",
    "native_sid_compatibility",
}
_LEGACY_CODEX_AUTHORITY_KEYS = {
    "codex_binary",
    "codex_config_overrides",
    "codex_profile",
    "provider_kind",
}
_PREWARM_TCP_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MAX_RUNTIME_AGENT_BYTES = 4 * 1024 * 1024
_MAX_RUNTIME_AGENTS = 128
_MAX_RUNTIME_AGENT_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_RUNTIME_AGENT_PAYLOAD_BYTES = 24 * 1024 * 1024
_RUNTIME_AGENT_PAYLOAD = "codex-runtime-agents.json"
_RUNTIME_AGENT_PAYLOAD_SCHEMA = 1


def codex_provider_contract(
    contract: CodexExecutionContract,
) -> dict[str, Any]:
    return {
        "type": "codex",
        "contract": json.loads(
            json.dumps(
                contract.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    }


def codex_contract_from_artifact(
    artifact: ExecutionArtifact,
) -> CodexExecutionContract:
    payload = artifact.provider_contract
    if (
        type(payload) is not dict
        or set(payload) != {"type", "contract"}
        or payload["type"] != "codex"
        or type(payload["contract"]) is not dict
    ):
        raise ExecutionAuthorityError(
            "Codex execution contract is unavailable",
        )
    contract = CodexExecutionContract.from_dict(payload["contract"])
    expected = (
        artifact.provider_id,
        artifact.provider_kind,
        artifact.provider_generation,
        artifact.provider_execution_revision,
    )
    actual = (
        contract.provider_id,
        contract.provider_kind,
        contract.provider_generation,
        contract.provider_execution_revision,
    )
    if actual != expected:
        raise ExecutionAuthorityError(
            "Codex execution contract conflicts with execution authority",
        )
    return contract


def codex_runner_launch_from_artifact(
    artifact: ExecutionArtifact,
) -> RunnerLaunch:
    raw = artifact.runtime_policy.get("runner_launch")
    if type(raw) is not dict:
        raise ExecutionAuthorityError(
            "Codex runner launch authority is unavailable",
        )
    try:
        launch = RunnerLaunch.from_dict(raw)
    except ExecutionContractError as exc:
        raise ExecutionAuthorityError(
            "Codex runner launch authority is invalid",
        ) from exc
    expected_run_dir = (
        Path(launch.launch.argv[2])
        if launch.frozen
        else Path(launch.launch.argv[3])
    )
    from runs_dir import runs_root

    if (
        launch.runner_kind != artifact.provider_kind
        or launch.runner_module != "runner_codex"
        or expected_run_dir
        != runs_root() / artifact.template.arguments()["run_id"]
        or not launch.attest()
    ):
        raise ExecutionAuthorityError(
            "Codex runner launch authority mismatch",
        )
    return launch


def retry_codex_execution(
    execution: PreparedExecution,
    **overrides: Any,
) -> PreparedExecution:
    if (
        not isinstance(execution, PreparedExecution)
        or execution.artifact.provider_kind not in {"codex", "fugu"}
    ):
        raise ExecutionAuthorityError("invalid Codex retry execution")
    artifact = execution.artifact
    arguments = execution.start_arguments()
    arguments.update(overrides)
    from runs_dir import runs_root

    runtime_policy = artifact.runtime_policy
    runtime_policy["runner_launch"] = retarget_runner_launch(
        codex_runner_launch_from_artifact(artifact),
        runs_root() / arguments["run_id"],
    ).to_dict()
    return prepare_execution(
        {
            "id": artifact.provider_id,
            "kind": artifact.provider_kind,
            "generation": artifact.provider_generation,
            "revision": artifact.provider_revision,
            "execution_revision": artifact.provider_execution_revision,
        },
        routing_session_id=artifact.routing_session_id,
        runtime_policy=runtime_policy,
        provider_contract=artifact.provider_contract,
        **arguments,
    )


def _read_attested_file(
    expected: FileIdentity,
    *,
    max_bytes: int | None = None,
) -> bytes:
    flags = binary_open_flags(
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(expected.resolved_path, flags)
        try:
            observed = os.fstat(fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_dev != expected.device
                or observed.st_ino != expected.inode
                or observed.st_size != expected.size
                or observed.st_mtime_ns != expected.mtime_ns
                or observed.st_ctime_ns != expected.ctime_ns
                or (
                    max_bytes is not None
                    and observed.st_size > max_bytes
                )
                or sha256_fd(fd) != expected.sha256
                or not expected.attest_metadata()
            ):
                raise ExecutionContractError("authority file mismatch")
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ExecutionContractError("authority file mismatch") from exc
    return b"".join(chunks)


def snapshot_codex_runtime_agents(
    *,
    enabled: bool,
) -> tuple[dict[str, Any] | None, bytes | None]:
    if not enabled:
        return None, None
    import extension_store

    entries = extension_store.runtime_agent_entries()
    if len(entries) > _MAX_RUNTIME_AGENTS:
        raise ExecutionContractError("too many Codex runtime agents")
    by_name: dict[str, bytes] = {}
    for entry in entries:
        source_raw = entry.get("codex")
        if not source_raw:
            continue
        source = Path(source_raw)
        name = source.name
        if (
            not name
            or name in {".", ".."}
            or Path(name).name != name
            or "\x00" in name
        ):
            raise ExecutionContractError("invalid Codex runtime agent")
        by_name[name] = _read_attested_file(
            FileIdentity.capture(source),
            max_bytes=_MAX_RUNTIME_AGENT_BYTES,
        )
    if not by_name:
        return None, None
    if sum(len(contents) for contents in by_name.values()) > (
        _MAX_RUNTIME_AGENT_TOTAL_BYTES
    ):
        raise ExecutionContractError("Codex runtime agents are too large")
    payload_agents = [
        {
            "name": name,
            "contents": base64.b64encode(by_name[name]).decode("ascii"),
        }
        for name in sorted(by_name)
    ]
    payload = json.dumps(
        {
            "schema": _RUNTIME_AGENT_PAYLOAD_SCHEMA,
            "agents": payload_agents,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest = {
        "path": _RUNTIME_AGENT_PAYLOAD,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "entries": [
            {
                "name": name,
                "sha256": hashlib.sha256(by_name[name]).hexdigest(),
                "size": len(by_name[name]),
            }
            for name in sorted(by_name)
        ],
    }
    return manifest, payload


def codex_runtime_agent_manifest(
    artifact: ExecutionArtifact,
) -> dict[str, Any] | None:
    raw = artifact.runtime_policy.get("runtime_agent_manifest")
    if raw is None:
        return None
    if (
        type(raw) is not dict
        or set(raw) != {"path", "sha256", "size", "entries"}
        or raw["path"] != _RUNTIME_AGENT_PAYLOAD
        or type(raw["sha256"]) is not str
        or len(raw["sha256"]) != 64
        or any(ch not in "0123456789abcdef" for ch in raw["sha256"])
        or type(raw["size"]) is not int
        or not 0 < raw["size"] <= _MAX_RUNTIME_AGENT_PAYLOAD_BYTES
        or type(raw["entries"]) is not list
        or not raw["entries"]
        or len(raw["entries"]) > _MAX_RUNTIME_AGENTS
    ):
        raise ExecutionAuthorityError("invalid Codex runtime agent manifest")
    names: set[str] = set()
    normalized_entries: list[dict[str, Any]] = []
    for item in raw["entries"]:
        if (
            type(item) is not dict
            or set(item) != {"name", "sha256", "size"}
            or type(item["name"]) is not str
            or not item["name"]
            or item["name"] in {".", ".."}
            or Path(item["name"]).name != item["name"]
            or item["name"] in names
            or type(item["sha256"]) is not str
            or len(item["sha256"]) != 64
            or any(
                ch not in "0123456789abcdef"
                for ch in item["sha256"]
            )
            or type(item["size"]) is not int
            or not 0 <= item["size"] <= _MAX_RUNTIME_AGENT_BYTES
        ):
            raise ExecutionAuthorityError(
                "invalid Codex runtime agent manifest",
            )
        names.add(item["name"])
        normalized_entries.append(dict(item))
    if [item["name"] for item in normalized_entries] != sorted(names):
        raise ExecutionAuthorityError(
            "invalid Codex runtime agent manifest",
        )
    if sum(item["size"] for item in normalized_entries) > (
        _MAX_RUNTIME_AGENT_TOTAL_BYTES
    ):
        raise ExecutionAuthorityError(
            "invalid Codex runtime agent manifest",
        )
    return {
        **raw,
        "entries": normalized_entries,
    }


def _staged_runtime_agent_dir(run_id: str) -> Path:
    from paths import bc_home

    key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return bc_home() / "prepared-execution-payloads" / key


def _write_private_payload(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=False)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = binary_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(temporary, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short runtime agent payload write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            path.parent.rmdir()
        except OSError:
            pass
        raise


def stage_codex_runtime_agent_payload(
    *,
    run_id: str,
    manifest: dict[str, Any] | None,
    payload: bytes | None,
) -> None:
    if manifest is None:
        if payload is not None:
            raise ExecutionAuthorityError(
                "Codex runtime agent payload lacks a manifest",
            )
        return
    if payload is None:
        raise ExecutionAuthorityError(
            "Codex runtime agent manifest lacks a payload",
        )
    if (
        len(payload) != manifest["size"]
        or hashlib.sha256(payload).hexdigest() != manifest["sha256"]
    ):
        raise ExecutionAuthorityError(
            "Codex runtime agent payload conflicts with its manifest",
        )
    _write_private_payload(
        _staged_runtime_agent_dir(run_id) / _RUNTIME_AGENT_PAYLOAD,
        payload,
    )


def cleanup_staged_codex_runtime_agent_payload(run_id: str) -> None:
    directory = _staged_runtime_agent_dir(run_id)
    try:
        (directory / _RUNTIME_AGENT_PAYLOAD).unlink()
    except FileNotFoundError:
        pass
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass


def install_staged_codex_runtime_agent_payload(
    run_dir: Path,
    *,
    run_id: str,
    manifest: dict[str, Any] | None,
) -> None:
    if manifest is None:
        return
    source_dir = _staged_runtime_agent_dir(run_id)
    source = source_dir / _RUNTIME_AGENT_PAYLOAD
    _read_codex_runtime_agent_payload(source, manifest)
    target = run_dir / _RUNTIME_AGENT_PAYLOAD
    if target.exists() or target.is_symlink():
        raise ExecutionAuthorityError(
            "Codex runtime agent payload target already exists",
        )
    os.replace(source, target)
    _read_codex_runtime_agent_payload(target, manifest)
    source_dir.rmdir()


def _read_codex_runtime_agent_payload(
    path: Path,
    manifest: dict[str, Any],
) -> tuple[bytes, tuple[tuple[str, bytes], ...]]:
    if path.is_symlink():
        raise ExecutionAuthorityError(
            "invalid Codex runtime agent payload",
        )
    flags = binary_open_flags(
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            observed = os.fstat(fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) & 0o077
                or (
                    hasattr(os, "getuid")
                    and observed.st_uid != os.getuid()
                )
                or observed.st_size != manifest["size"]
            ):
                raise ExecutionAuthorityError(
                    "invalid Codex runtime agent payload",
                )
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ExecutionAuthorityError(
            "invalid Codex runtime agent payload",
        ) from exc
    payload = b"".join(chunks)
    if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
        raise ExecutionAuthorityError(
            "invalid Codex runtime agent payload",
        )
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionAuthorityError(
            "invalid Codex runtime agent payload",
        ) from exc
    if (
        type(decoded) is not dict
        or set(decoded) != {"schema", "agents"}
        or decoded["schema"] != _RUNTIME_AGENT_PAYLOAD_SCHEMA
        or type(decoded["agents"]) is not list
        or len(decoded["agents"]) != len(manifest["entries"])
    ):
        raise ExecutionAuthorityError(
            "invalid Codex runtime agent payload",
        )
    agents: list[tuple[str, bytes]] = []
    for item, expected in zip(decoded["agents"], manifest["entries"]):
        if (
            type(item) is not dict
            or set(item) != {"name", "contents"}
            or item["name"] != expected["name"]
            or type(item["contents"]) is not str
        ):
            raise ExecutionAuthorityError(
                "invalid Codex runtime agent payload",
            )
        try:
            contents = base64.b64decode(
                item["contents"],
                validate=True,
            )
        except ValueError as exc:
            raise ExecutionAuthorityError(
                "invalid Codex runtime agent payload",
            ) from exc
        if (
            len(contents) != expected["size"]
            or hashlib.sha256(contents).hexdigest()
            != expected["sha256"]
        ):
            raise ExecutionAuthorityError(
                "invalid Codex runtime agent payload",
            )
        agents.append((expected["name"], contents))
    return payload, tuple(agents)


def read_codex_runtime_agent_payload(
    run_dir: Path,
    manifest: dict[str, Any] | None,
) -> tuple[tuple[str, bytes], ...]:
    if manifest is None:
        return ()
    path = run_dir / _RUNTIME_AGENT_PAYLOAD
    _, agents = _read_codex_runtime_agent_payload(path, manifest)
    return agents


def clone_codex_runtime_agent_payload(
    source_run_dir: Path,
    *,
    target_run_id: str,
    manifest: dict[str, Any] | None,
) -> None:
    if manifest is None:
        return
    payload, _ = _read_codex_runtime_agent_payload(
        source_run_dir / _RUNTIME_AGENT_PAYLOAD,
        manifest,
    )
    stage_codex_runtime_agent_payload(
        run_id=target_run_id,
        manifest=manifest,
        payload=payload,
    )


def cleanup_installed_codex_runtime_agent_payload(run_dir: Path) -> None:
    try:
        (run_dir / _RUNTIME_AGENT_PAYLOAD).unlink()
    except FileNotFoundError:
        pass


def codex_runtime_policy(artifact: ExecutionArtifact) -> dict[str, Any]:
    policy = artifact.runtime_policy
    if type(policy) is not dict or set(policy) != _RUNTIME_POLICY_KEYS:
        raise ExecutionAuthorityError("invalid Codex runtime policy")
    if (
        type(policy["permission"]) is not dict
        or type(policy["run_policy"]) is not dict
        or type(policy["request_user_input_enabled"]) is not bool
        or type(policy["disabled_runtime_skills"]) is not list
        or any(type(value) is not str for value in policy["disabled_runtime_skills"])
    ):
        raise ExecutionAuthorityError("invalid Codex runtime policy")
    for key in ("context_strategy", "working_mode", "worker_working_mode"):
        if policy[key] is not None and type(policy[key]) is not str:
            raise ExecutionAuthorityError("invalid Codex runtime policy")
    return policy


def attested_config_files(
    contract: CodexExecutionContract,
) -> tuple[tuple[Path, bytes | None], ...]:
    materialized: list[tuple[Path, bytes | None]] = []
    for identity in contract.config:
        root = Path(identity.root_path)
        candidate = Path(identity.config_path)
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ExecutionContractError(
                "config path escapes its root",
            ) from exc
        if relative.is_absolute() or ".." in relative.parts:
            raise ExecutionContractError("config path escapes its root")
        if identity.config_file is None:
            if not identity.attest():
                raise ExecutionContractError(
                    "config identity mismatch",
                )
            materialized.append((relative, None))
            continue
        contents = _read_attested_file(identity.config_file)
        if not identity.attest_metadata():
            raise ExecutionContractError("config identity mismatch")
        materialized.append((relative, contents))
    return tuple(materialized)


def _validated_prewarm_ready(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if type(value) is not dict or len(value) > 128:
        raise ExecutionAuthorityError("invalid MCP prewarm hydration")
    ready: dict[str, Any] = {}
    for server_name, endpoint in value.items():
        if (
            type(server_name) is not str
            or not server_name
            or len(server_name) > 256
        ):
            raise ExecutionAuthorityError("invalid MCP prewarm hydration")
        if endpoint is None:
            ready[server_name] = None
            continue
        if type(endpoint) is str:
            candidate = Path(endpoint)
            if (
                not candidate.is_absolute()
                or candidate.is_symlink()
                or "\x00" in endpoint
            ):
                raise ExecutionAuthorityError(
                    "invalid MCP prewarm hydration",
                )
            try:
                resolved = candidate.resolve(strict=True)
                observed = resolved.stat()
                from paths import bc_home

                allowed_roots = (
                    (bc_home() / "mcp_daemons").resolve(strict=False),
                    (
                        Path(tempfile.gettempdir())
                        / f"ba-mcp-{os.getuid()}"
                    ).resolve(strict=False),
                )
            except OSError as exc:
                raise ExecutionAuthorityError(
                    "invalid MCP prewarm hydration",
                ) from exc
            if (
                not stat.S_ISSOCK(observed.st_mode)
                or observed.st_uid != os.getuid()
                or not any(
                    resolved.is_relative_to(root)
                    for root in allowed_roots
                )
            ):
                raise ExecutionAuthorityError(
                    "invalid MCP prewarm hydration",
                )
            ready[server_name] = str(resolved)
            continue
        if (
            type(endpoint) is not dict
            or set(endpoint)
            != {"transport", "host", "port", "connect_secret"}
            or endpoint["transport"] != "tcp"
            or endpoint["host"] not in _PREWARM_TCP_HOSTS
            or type(endpoint["port"]) is not int
            or not 1 <= endpoint["port"] <= 65535
            or type(endpoint["connect_secret"]) is not str
            or not endpoint["connect_secret"]
            or len(endpoint["connect_secret"]) > 4096
        ):
            raise ExecutionAuthorityError("invalid MCP prewarm hydration")
        ready[server_name] = dict(endpoint)
    return ready


def restore_codex_runner_inputs(
    run_dir: Path,
    hydrated_input: Mapping[str, Any],
) -> tuple[dict[str, Any], CodexExecutionContract]:
    if type(hydrated_input) is not dict:
        raise ExecutionAuthorityError("Codex runner input must be an object")
    artifact = load_execution_artifact(run_dir, validate_input=False)
    validate_execution_input_projection(artifact, hydrated_input)
    if artifact.provider_kind not in {"codex", "fugu"}:
        raise ExecutionAuthorityError("Codex execution provider is invalid")
    if set(hydrated_input) & _LEGACY_CODEX_AUTHORITY_KEYS:
        raise ExecutionAuthorityError(
            "runner input contains a second Codex authority",
        )
    policy = codex_runtime_policy(artifact)
    contract = codex_contract_from_artifact(artifact)
    volatile = {
        key: hydrated_input.get(key)
        for key in ("backend_url", "extra_env", "internal_token")
    }
    volatile["_mcp_prewarm_ready"] = _validated_prewarm_ready(
        hydrated_input.get("_mcp_prewarm_ready"),
    )
    restored = {
        **artifact.template.arguments(),
        **volatile,
        **policy["run_policy"],
        "_codex_runtime_agent_manifest": codex_runtime_agent_manifest(
            artifact,
        ),
        "context_strategy": policy["context_strategy"],
        "disabled_runtime_skills": policy["disabled_runtime_skills"],
        "permission": policy["permission"],
        "provider_id": artifact.provider_id,
        "provider_kind": artifact.provider_kind,
        "request_user_input_enabled": policy[
            "request_user_input_enabled"
        ],
        "worker_working_mode": policy["worker_working_mode"],
        "working_mode": policy["working_mode"],
    }
    return restored, contract

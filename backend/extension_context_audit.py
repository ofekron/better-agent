from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import portable_lock
import provisioning
from paths import bc_home
from provisioning import DirtyPolicy, ProvisionedSessionSpec
from prompt_templates import render_prompt

logger = logging.getLogger(__name__)

AUDIT_CONTEXT_NAME = "Dynamic Harness Audit"
AUDIT_CONTEXT_CATEGORY = "dynamic"
AUDIT_SPEC_KEY = "extension_context_audit"
_CACHE_SCHEMA_VERSION = 2
_AUDIT_VERSION = 2
_MAX_CACHE_ENTRIES = 64
_MAX_FINDINGS = 12
_MAX_REFS_PER_FINDING = 2
_MAX_RETRY_SECONDS = 300.0
_INITIAL_RETRY_SECONDS = 5.0
_REFRESH_LOCK = threading.Lock()
_CACHE_PROJECTION_LOCK = threading.Lock()
_IN_FLIGHT: set[str] = set()
_FAILURES: dict[str, tuple[int, float]] = {}
_CACHE_PROJECTION: dict[str, dict[str, Any]] = {}
_SAFE_DISPLAY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_REF_RE = re.compile(r"(skill|mcp|instruction|capability):[0-9a-f]{64}")
_CODE_FENCE_RE = re.compile(r"\A```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*\Z", re.DOTALL)
_FINDING_RULES = {
    "potential_overlap": {
        "kinds": frozenset({"skill", "mcp", "instruction", "capability"}),
        "arity": 2,
        "severity": "low",
    },
    "identifier_clarity": {
        "kinds": frozenset({"skill", "mcp", "instruction", "capability"}),
        "arity": 1,
        "severity": "low",
    },
}


class ExtensionContextAuditSpec(ProvisionedSessionSpec):
    key = AUDIT_SPEC_KEY
    version = _AUDIT_VERSION
    name = "extension-context-auditor"
    env_prefix = "EXTENSION_CONTEXT_AUDIT"
    task_key = "extension_context_audit"
    orchestration_mode = "native"
    bare_config = True
    worker_creation_policy = "deny"
    machine_completion = True
    run_mode = "fork"
    dispatch = "in_process"
    on_no_fork = "error"
    dirty_policy = DirtyPolicy(max_base_bytes=300_000, max_user_turns=1, max_assistant_turns=1)
    provision_timeout = 120.0

    def build_provision_prompt(self, ctx: dict) -> str:
        return render_prompt("provisioning/extension_context_auditor.md", {})

    def build_instructions(self, query: str, ctx: dict) -> str:
        return f"<active-contextual-harness-mix>\n{query}\n</active-contextual-harness-mix>"

    def parse_result(self, text: str, ctx: dict) -> dict:
        projection = ctx.get("projection")
        if not isinstance(projection, dict):
            raise ValueError("audit projection is required")
        return _validate_audit_result(_parse_json_object(text), projection)


AUDIT_SPEC = provisioning.register(ExtensionContextAuditSpec())


def build_projection(
    *,
    provider_kind: str,
    profile_id: str,
    profile_revision: str,
    runtime_skills: list[dict[str, str]],
    capability_contexts: list[dict[str, str]],
    extension_mcp_servers: dict[str, list[str]],
    extension_skills: dict[str, list[str]],
    extension_instruction_names: dict[str, list[str]],
) -> dict[str, Any]:
    entries: dict[str, dict[str, str]] = {}
    identities: dict[str, dict[str, Any]] = {}

    def add(kind: str, identity: Any, display: str = "") -> None:
        canonical = _canonical_json({"kind": kind, "identity": identity})
        ref = f"{kind}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        entry = {"ref": ref, "kind": kind}
        if _SAFE_DISPLAY_RE.fullmatch(display):
            entry["display"] = display
        entries[ref] = entry
        identities[ref] = {"kind": kind, "identity": identity}

    for item in runtime_skills:
        if not isinstance(item, dict):
            raise ValueError("runtime skill identity must be an object")
        name = _identity_string(item.get("name"), "runtime skill name")
        add("skill", {"source": "runtime", "name": name}, name)
    for item in capability_contexts:
        if not isinstance(item, dict):
            raise ValueError("capability identity must be an object")
        source_id = _identity_string(item.get("source_id"), "capability source id")
        capability_id = _identity_string(item.get("capability_id"), "capability id")
        add(
            "capability",
            {"source_id": source_id, "capability_id": capability_id},
            capability_id,
        )
    _add_extension_entries(add, "mcp", extension_mcp_servers)
    _add_extension_entries(add, "skill", extension_skills)
    _add_extension_entries(add, "instruction", extension_instruction_names)
    return {
        "version": _AUDIT_VERSION,
        "provider_kind": _bounded_string(provider_kind, "provider_kind"),
        "profile_id": _bounded_string(profile_id, "profile_id"),
        "profile_revision": _bounded_string(
            profile_revision,
            "profile_revision",
            required=False,
        ),
        "entries": [entries[ref] for ref in sorted(entries)],
        "identities": {ref: identities[ref] for ref in sorted(identities)},
    }


def _add_extension_entries(
    add: Callable[[str, Any, str], None],
    kind: str,
    values: dict[str, list[str]],
) -> None:
    if not isinstance(values, dict):
        raise ValueError(f"extension {kind} identities must be an object")
    for raw_extension_id, raw_names in values.items():
        extension_id = _identity_string(raw_extension_id, f"extension {kind} owner")
        if not isinstance(raw_names, list):
            raise ValueError(f"extension {kind} identities must be lists")
        for raw_name in raw_names:
            name = _identity_string(raw_name, f"extension {kind} name")
            add(kind, {"extension_id": extension_id, "name": name}, f"{extension_id}:{name}")


def _identity_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_000:
        raise ValueError(f"{field} must be a non-empty bounded string")
    return value


def runtime_context(
    projection: dict[str, Any],
    *,
    bare_config: bool = False,
    request_refresh: bool = True,
) -> list[dict[str, str]]:
    if bare_config:
        return []
    normalized = _validate_projection(projection)
    fingerprint = _fingerprint(normalized)
    cached = _read_cache_cached(fingerprint, normalized)
    if not cached:
        if request_refresh:
            _trigger_refresh(fingerprint, normalized)
        return []
    content = _render_context(cached["result"], normalized)
    if not content:
        return []
    return [{
        "name": AUDIT_CONTEXT_NAME,
        "category": AUDIT_CONTEXT_CATEGORY,
        "content_kind": "dynamic_harness_audit",
        "content": content,
    }]


def _validate_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "provider_kind",
        "profile_id",
        "profile_revision",
        "entries",
        "identities",
    }:
        raise ValueError("invalid audit projection")
    if value.get("version") != _AUDIT_VERSION:
        raise ValueError("unsupported audit projection version")
    entries = value.get("entries")
    identities = value.get("identities")
    if not isinstance(entries, list) or not isinstance(identities, dict):
        raise ValueError("audit projection entries and identities are required")
    normalized_entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict) or not set(item).issubset({"ref", "kind", "display"}):
            raise ValueError("invalid audit projection entry")
        ref = item.get("ref")
        kind = item.get("kind")
        display = item.get("display")
        if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
            raise ValueError("invalid audit projection ref")
        if kind != ref.partition(":")[0] or ref in seen:
            raise ValueError("inconsistent audit projection ref")
        if display is not None and (
            not isinstance(display, str) or not _SAFE_DISPLAY_RE.fullmatch(display)
        ):
            raise ValueError("invalid audit projection display")
        normalized = {"ref": ref, "kind": kind}
        if display is not None:
            normalized["display"] = display
        normalized_entries.append(normalized)
        seen.add(ref)
    normalized_identities: dict[str, dict[str, Any]] = {}
    if set(identities) != seen:
        raise ValueError("audit projection identities must match entries")
    for ref in sorted(identities):
        item = identities[ref]
        if not isinstance(item, dict) or set(item) != {"kind", "identity"}:
            raise ValueError("invalid audit projection identity")
        kind = item.get("kind")
        identity = _validate_original_identity(kind, item.get("identity"))
        expected_ref = (
            f"{kind}:"
            f"{hashlib.sha256(_canonical_json({'kind': kind, 'identity': identity}).encode('utf-8')).hexdigest()}"
        )
        if ref != expected_ref:
            raise ValueError("audit projection identity does not match ref")
        normalized_identities[ref] = {"kind": kind, "identity": identity}
    normalized_entries.sort(key=lambda item: item["ref"])
    return {
        "version": _AUDIT_VERSION,
        "provider_kind": _bounded_string(value.get("provider_kind"), "provider_kind"),
        "profile_id": _bounded_string(value.get("profile_id"), "profile_id"),
        "profile_revision": _bounded_string(
            value.get("profile_revision"),
            "profile_revision",
            required=False,
        ),
        "entries": normalized_entries,
        "identities": normalized_identities,
    }


def _validate_original_identity(kind: Any, identity: Any) -> dict[str, str]:
    if kind == "capability":
        expected = {"source_id", "capability_id"}
    elif kind == "skill" and isinstance(identity, dict) and identity.get("source") == "runtime":
        expected = {"source", "name"}
    elif kind in {"skill", "mcp", "instruction"}:
        expected = {"extension_id", "name"}
    else:
        raise ValueError("invalid audit projection identity kind")
    if not isinstance(identity, dict) or set(identity) != expected:
        raise ValueError("invalid audit projection identity shape")
    return {
        key: _identity_string(identity.get(key), f"audit identity {key}")
        for key in sorted(expected)
    }


def _bounded_string(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if required and not value:
        raise ValueError(f"{field} must be non-empty")
    if len(value) > 1_000:
        raise ValueError(f"{field} is too large")
    return value


def _trigger_refresh(fingerprint: str, projection: dict[str, Any]) -> None:
    current = time.monotonic()
    with _REFRESH_LOCK:
        if fingerprint in _IN_FLIGHT:
            return
        failure = _FAILURES.get(fingerprint)
        if failure is not None and current < failure[1]:
            return
        _IN_FLIGHT.add(fingerprint)
    threading.Thread(
        target=_refresh_cache,
        args=(fingerprint, projection),
        name="extension-context-audit-refresh",
        daemon=True,
    ).start()


def _refresh_cache(fingerprint: str, projection: dict[str, Any]) -> None:
    try:
        payload = _canonical_json(_llm_projection(projection))
        result = provisioning.run_sync(
            AUDIT_SPEC,
            payload,
            {"fingerprint": fingerprint, "projection": projection},
        )
        validated = _validate_audit_result(result.value, projection)
        _write_cache(fingerprint, validated, projection)
        with _REFRESH_LOCK:
            _FAILURES.pop(fingerprint, None)
    except Exception:
        with _REFRESH_LOCK:
            attempts = _FAILURES.get(fingerprint, (0, 0.0))[0] + 1
            exponent = min(attempts - 1, 10)
            delay = min(_MAX_RETRY_SECONDS, _INITIAL_RETRY_SECONDS * (2 ** exponent))
            _FAILURES[fingerprint] = (attempts, time.monotonic() + delay)
        logger.exception("extension context audit refresh failed")
    finally:
        with _REFRESH_LOCK:
            _IN_FLIGHT.discard(fingerprint)


def _llm_projection(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": projection["version"],
        "provider_kind": (
            projection["provider_kind"]
            if _SAFE_DISPLAY_RE.fullmatch(projection["provider_kind"])
            else ""
        ),
        "profile": (
            projection["profile_id"]
            if _SAFE_DISPLAY_RE.fullmatch(projection["profile_id"])
            else ""
        ),
        "entries": projection["entries"],
    }


def _fingerprint(projection: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(projection).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _cache_dir() -> Path:
    return bc_home() / "extension_context_audits"


def _cache_path(fingerprint: str) -> Path:
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("invalid audit fingerprint")
    return _cache_dir() / f"{fingerprint}.json"


def _read_cache_cached(
    fingerprint: str,
    projection: dict[str, Any],
) -> dict[str, Any]:
    with _CACHE_PROJECTION_LOCK:
        cached = _CACHE_PROJECTION.get(fingerprint)
    if cached is not None:
        if _cache_path(fingerprint).is_file():
            return cached
        with _CACHE_PROJECTION_LOCK:
            _CACHE_PROJECTION.pop(fingerprint, None)
    data = _read_cache(fingerprint, projection)
    if data:
        _remember_cache(fingerprint, data)
    return data


def _read_cache(
    fingerprint: str,
    projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        data = json.loads(_cache_path(fingerprint).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "audit_version", "fingerprint", "result"}
        or data.get("schema_version") != _CACHE_SCHEMA_VERSION
        or data.get("audit_version") != _AUDIT_VERSION
        or data.get("fingerprint") != fingerprint
    ):
        return {}
    if projection is not None:
        try:
            data["result"] = _validate_audit_result(data.get("result"), projection)
        except ValueError:
            return {}
    return data


def _write_cache(
    fingerprint: str,
    result: dict[str, Any],
    projection: dict[str, Any],
) -> None:
    projection = _validate_projection(projection)
    if fingerprint != _fingerprint(projection):
        raise ValueError("cache fingerprint does not match projection")
    result = _validate_audit_result(result, projection)
    directory = _cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".cache.lock"
    with lock_path.open("a+b") as lock_file:
        portable_lock.lock_ex(lock_file.fileno())
        try:
            data = {
                "schema_version": _CACHE_SCHEMA_VERSION,
                "audit_version": _AUDIT_VERSION,
                "fingerprint": fingerprint,
                "result": result,
            }
            path = _cache_path(fingerprint)
            _atomic_write_json(path, data)
            live_fingerprints = _evict_cache_entries(directory, keep=path)
            _remember_cache(fingerprint, data, live_fingerprints=live_fingerprints)
        finally:
            portable_lock.unlock(lock_file.fileno())


def _remember_cache(
    fingerprint: str,
    data: dict[str, Any],
    *,
    live_fingerprints: set[str] | None = None,
) -> None:
    with _CACHE_PROJECTION_LOCK:
        _CACHE_PROJECTION[fingerprint] = data
        if live_fingerprints is not None:
            for cached_fingerprint in list(_CACHE_PROJECTION):
                if cached_fingerprint not in live_fingerprints:
                    _CACHE_PROJECTION.pop(cached_fingerprint, None)
        while len(_CACHE_PROJECTION) > _MAX_CACHE_ENTRIES:
            victim = next(
                key for key in sorted(_CACHE_PROJECTION)
                if key != fingerprint
            )
            _CACHE_PROJECTION.pop(victim, None)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=True, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _evict_cache_entries(directory: Path, *, keep: Path) -> set[str]:
    entries: list[tuple[int, str, Path]] = []
    for path in directory.glob("*.json"):
        if not _FINGERPRINT_RE.fullmatch(path.stem):
            continue
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        entries.append((mtime, path.name, path))
    entries.sort()
    removable = [entry for entry in entries if entry[2] != keep]
    excess = max(0, len(entries) - _MAX_CACHE_ENTRIES)
    removed: set[str] = set()
    for _mtime, _name, path in removable[:excess]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        removed.add(path.stem)
    return {path.stem for _mtime, _name, path in entries} - removed


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        stripped = text.strip()
    except AttributeError:
        raise ValueError("audit result must be one JSON object")
    fence_match = _CODE_FENCE_RE.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        raise ValueError("audit result must be one JSON object")
    if not isinstance(value, dict):
        raise ValueError("audit result must be one JSON object")
    return value


def _validate_audit_result(value: Any, projection: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"status", "findings"}:
        raise ValueError("invalid audit result")
    status = value.get("status")
    findings = value.get("findings")
    if status not in {"clean", "findings"} or not isinstance(findings, list):
        raise ValueError("invalid audit status")
    if status == "clean" and findings:
        raise ValueError("clean audit cannot contain findings")
    if status == "findings" and not findings:
        raise ValueError("findings audit must contain findings")
    if len(findings) > _MAX_FINDINGS:
        raise ValueError("too many audit findings")
    ref_kinds = {
        ref: item["kind"]
        for ref, item in projection["identities"].items()
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in findings:
        if not isinstance(item, dict) or set(item) != {"code", "refs"}:
            raise ValueError("invalid audit finding")
        code = item.get("code")
        refs = item.get("refs")
        rule = _FINDING_RULES.get(code)
        if rule is None or not isinstance(refs, list) or len(refs) != rule["arity"]:
            raise ValueError("invalid audit finding rule")
        if len(refs) > _MAX_REFS_PER_FINDING or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("invalid audit finding refs")
        if len(set(refs)) != len(refs):
            raise ValueError("audit finding refs must be distinct")
        if any(ref_kinds.get(ref) not in rule["kinds"] for ref in refs):
            raise ValueError("audit finding ref is unavailable")
        key = (code, tuple(sorted(refs)))
        if key in seen:
            raise ValueError("duplicate audit finding")
        seen.add(key)
        normalized.append({"code": code, "refs": list(refs)})
    normalized.sort(key=lambda item: (item["code"], item["refs"]))
    return {"status": status, "findings": normalized}


def _render_context(result: dict[str, Any], projection: dict[str, Any]) -> str:
    validated = _validate_audit_result(result, projection)
    if validated["status"] == "clean":
        return ""
    labels = {
        item["ref"]: item.get("display") or item["ref"][:24]
        for item in projection["entries"]
    }
    lines = [
        "Advisory harness review. These are review prompts, not instructions or verified defects.",
        "",
    ]
    for finding in validated["findings"]:
        refs = [labels[ref] for ref in finding["refs"]]
        rule = _FINDING_RULES[finding["code"]]
        if finding["code"] == "potential_overlap":
            message = f"Consider whether {refs[0]} and {refs[1]} overlap."
        else:
            message = f"Consider whether {refs[0]} is clear enough to distinguish its purpose."
        lines.append(f"- {rule['severity']}: {message}")
    return "\n".join(lines)

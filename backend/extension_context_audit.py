from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import provisioning
from paths import bc_home
from provisioning import DirtyPolicy, ProvisionedSessionSpec
from provisioning.prompts import render_prompt

logger = logging.getLogger(__name__)

AUDIT_CONTEXT_NAME = "Dynamic Harness Audit"
AUDIT_CONTEXT_CATEGORY = "dynamic"
AUDIT_SPEC_KEY = "extension_context_audit"
_CACHE_SCHEMA_VERSION = 1
_AUDIT_VERSION = 1
_MAX_TEXT = 500
_MAX_ITEMS = 80
_REFRESH_LOCK = threading.Lock()
_IN_FLIGHT: set[str] = set()
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


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
        return render_prompt("extension_context_auditor.md", {})

    def build_instructions(self, query: str, ctx: dict) -> str:
        return (
            "<extension-harness-inventory>\n"
            f"{query}\n"
            "</extension-harness-inventory>"
        )

    def parse_result(self, text: str, ctx: dict) -> dict:
        return _normalize_audit_result(_parse_json_object(text))


AUDIT_SPEC = provisioning.register(ExtensionContextAuditSpec())


def runtime_context(cwd: str, *, bare_config: bool = False) -> list[dict[str, str]]:
    if bare_config or not _is_runtime_ready():
        return []
    inventory = build_inventory(cwd)
    fingerprint = _fingerprint(inventory)
    cached = _read_cache()
    if cached.get("fingerprint") != fingerprint:
        _trigger_refresh(fingerprint, inventory)
        return []
    content = _render_context(cached.get("result"))
    if not content:
        return []
    return [{
        "name": AUDIT_CONTEXT_NAME,
        "category": AUDIT_CONTEXT_CATEGORY,
        "content_kind": "dynamic_harness_audit",
        "content": content,
    }]


def build_inventory(cwd: str) -> dict[str, Any]:
    import extension_store
    import runtime_skills

    extensions: list[dict[str, Any]] = []
    for record in extension_store.list_extensions(include_hidden=True):
        manifest = record.get("manifest") or {}
        extension_id = _clean(manifest.get("id"))
        entrypoints = manifest.get("entrypoints") or {}
        extensions.append({
            "id": extension_id,
            "name": _clean(manifest.get("name") or extension_id),
            "enabled": record.get("enabled") is True,
            "surfaces": _clean_list(manifest.get("surfaces") or []),
            "permissions": sorted(k for k, v in extension_store.effective_permissions(record).items() if v),
            "instructions": _instruction_items(entrypoints),
            "skills": _entrypoint_items(entrypoints.get("skills") or []),
            "mcp": _mcp_items(extension_store.extension_mcp_servers(extension_id) if extension_id else []),
            "remote_services": _entrypoint_items(entrypoints.get("remote_services") or []),
            "harness_delivery": extension_store.harness_delivery_mode(extension_id) if extension_id else "",
        })
    return {
        "version": _AUDIT_VERSION,
        "cwd": _clean(cwd, max_chars=300),
        "extensions": extensions[:_MAX_ITEMS],
        "runtime_skills": [
            {
                "name": _clean(skill.get("name")),
                "description": _clean(skill.get("description")),
            }
            for skill in runtime_skills._discover_skills(cwd)[:_MAX_ITEMS]
        ],
    }


def _instruction_items(entrypoints: dict[str, Any]) -> list[dict[str, str]]:
    items = entrypoints.get("instructions")
    if items is None:
        items = [{**i, "level": "global"} for i in entrypoints.get("provider_capabilities") or [] if isinstance(i, dict)]
    out: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "name": _clean(item.get("name")),
            "level": "project" if item.get("level") == "project" else "global",
        })
    return out[:_MAX_ITEMS]


def _entrypoint_items(items: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            out.append({"name": _clean(item)})
        elif isinstance(item, dict):
            out.append({
                "name": _clean(item.get("name") or item.get("id") or item.get("label")),
                "purpose": _clean(item.get("purpose") or item.get("description")),
            })
    return out[:_MAX_ITEMS]


def _mcp_items(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        out.append({
            "name": _clean(item.get("name")),
            "label": _clean(item.get("label")),
            "enabled": item.get("enabled") is True,
            "user_facing": item.get("user_facing") is not False,
        })
    return out[:_MAX_ITEMS]


def _trigger_refresh(fingerprint: str, inventory: dict[str, Any]) -> None:
    with _REFRESH_LOCK:
        if fingerprint in _IN_FLIGHT:
            return
        _IN_FLIGHT.add(fingerprint)
    thread = threading.Thread(
        target=_refresh_cache,
        args=(fingerprint, inventory),
        name="extension-context-audit-refresh",
        daemon=True,
    )
    thread.start()


def _refresh_cache(fingerprint: str, inventory: dict[str, Any]) -> None:
    try:
        payload = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result = provisioning.run_sync(AUDIT_SPEC, payload, {"fingerprint": fingerprint})
        _write_cache({"schema_version": _CACHE_SCHEMA_VERSION, "fingerprint": fingerprint, "result": result.value})
    except Exception:
        logger.exception("extension context audit refresh failed")
    finally:
        with _REFRESH_LOCK:
            _IN_FLIGHT.discard(fingerprint)


def _fingerprint(inventory: dict[str, Any]) -> str:
    payload = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_runtime_ready() -> bool:
    import config_store
    resolved = config_store.resolve_internal_llm(AUDIT_SPEC_KEY)
    return bool(resolved.get("provider_id") and resolved.get("model"))


def _cache_path() -> Path:
    return bc_home() / "extension_context_audit.json"


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return {}
    return data


def _write_cache(data: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _parse_json_object(text: str) -> dict[str, Any]:
    match = _JSON_OBJECT_RE.search(text or "")
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_audit_result(value: dict[str, Any]) -> dict[str, Any]:
    attention = []
    for item in value.get("attention") if isinstance(value.get("attention"), list) else []:
        if not isinstance(item, dict):
            continue
        severity = _clean(item.get("severity")).lower()
        if severity not in {"low", "medium", "high"}:
            severity = "medium"
        title = _clean(item.get("title"), max_chars=120)
        reason = _clean(item.get("reason"), max_chars=300)
        if title and reason:
            attention.append({"severity": severity, "title": title, "reason": reason})
    guidance = [
        _clean(item, max_chars=180)
        for item in value.get("tool_guidance", [])
        if isinstance(item, str) and _clean(item, max_chars=180)
    ]
    return {
        "summary": _clean(value.get("summary"), max_chars=650),
        "attention": attention[:6],
        "tool_guidance": guidance[:8],
    }


def _render_context(value: Any) -> str:
    result = _normalize_audit_result(value if isinstance(value, dict) else {})
    lines: list[str] = []
    if result["summary"]:
        lines.append(result["summary"])
    if result["tool_guidance"]:
        lines.append("")
        lines.append("Tool mix guidance:")
        lines.extend(f"- {item}" for item in result["tool_guidance"])
    if result["attention"]:
        lines.append("")
        lines.append("Needs user attention:")
        lines.extend(
            f"- {item['severity']}: {item['title']} — {item['reason']}"
            for item in result["attention"]
        )
    return "\n".join(lines).strip()


def _clean(value: Any, *, max_chars: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:max_chars]


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item) for item in value if isinstance(item, str) and _clean(item)][:_MAX_ITEMS]

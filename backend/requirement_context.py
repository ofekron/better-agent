from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from copy import copy
from pathlib import Path
from typing import Any

import extension_jobs
import provisioning
import extension_package_loader
import extension_store

logger = logging.getLogger(__name__)

RG_TIMEOUT_SECONDS = 30
DEFAULT_MATCH_FIELDS = ("text", "kind", "origin", "polarity", "strength", "source", "cwd", "ts")
MATCH_FIELD_ORDER = (
    "source_key",
    "source_prompt_key",
    "unit_index",
    "text",
    "kind",
    "origin",
    "polarity",
    "strength",
    "source",
    "source_text",
    "prev_reply",
    "cwd",
    "edited_files",
    "git_commits",
    "sid",
    "path",
    "ts",
    "user_seq",
    "native_hit_index",
)
PROMPT_FALLBACK_KIND = "unprocessed_prompt"
NATIVE_TRANSCRIPT_BUNDLE_KIND = "native_transcript_bundle"
GET_REQUIREMENTS_PROCESSOR_KEY = "get_requirements_processor"
# One normal dispatch plus one extra only when the fork's evidence tools were
# transiently unavailable (post-restart MCP not reconnected) and local search works.
PROCESSOR_ATTEMPTS = 2
NATIVE_BUNDLE_HIT_LIMIT = 6
NATIVE_BUNDLE_WINDOW_BEFORE = 5
NATIVE_BUNDLE_COLD_RETRY_TIMEOUT_SECONDS = 20.0
# Full-recall SQL has a generous execution deadline but remains subject to the
# native index's explicit result-byte budget; oversized results fail atomically.
NATIVE_INDEX_SQL_TIMEOUT_SECONDS = 120.0
NATIVE_BUNDLE_WINDOW_AFTER = 8
NATIVE_BUNDLE_EXACT_COLLAPSE_MIN_CHARS = 256
NATIVE_BUNDLE_PREFIX_COLLAPSE_FIELDS = (
    ("prefix_8192_sha256", 8192),
    ("prefix_4096_sha256", 4096),
    ("prefix_1024_sha256", 1024),
)
UNIT_FTS_DB_NAME = "requirement_units_fts.sqlite3"
UNIT_VECTOR_DB_NAME = "requirement_units_vectors.npz"
UNIT_VECTOR_STATE_NAME = "requirement_units_vectors.state.json"
UNIT_FTS_TOKEN_RE = re.compile(r"[\w-]{2,}", re.UNICODE)
RG_QUERY_MAX_CHARS = 4000
RG_QUERY_MAX_PATTERNS = 128
RG_FORBIDDEN_OPTIONS = {
    "-f",
    "--file",
    "--pre",
    "--pre-glob",
    "--config",
    "--ignore-file",
    "--ignore-file-case-insensitive",
}
RG_OPTIONS_WITH_VALUE = {
    "-A",
    "-B",
    "-C",
    "-E",
    "-e",
    "-g",
    "-m",
    "--after-context",
    "--before-context",
    "--context",
    "--context-separator",
    "--engine",
    "--field-context-separator",
    "--field-match-separator",
    "--glob",
    "--iglob",
    "--max-count",
    "--max-depth",
    "--path-separator",
    "--regexp",
    "--regex-size-limit",
    "--replace",
    "--sort",
    "--sortr",
    "--type",
    "--type-add",
    "--type-clear",
    "--type-not",
}


class _ProvisionedSpecHandle:
    def __init__(self, key: str, module_name: str) -> None:
        self.key = key
        self._module_name = module_name

    def _resolve(self):
        return _get_provisioned_spec(self.key, self._module_name)

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)


def _get_provisioned_spec(key: str, module_name: str):
    try:
        return provisioning.get(key)
    except KeyError:
        pass
    try:
        _ensure_requirements_importable()
        importlib.import_module(module_name)
    except Exception as exc:
        raise RuntimeError(f"provisioned spec {key!r} is unavailable") from exc
    try:
        return provisioning.get(key)
    except KeyError as exc:
        raise RuntimeError(f"provisioned spec {key!r} was not registered") from exc


def _get_requirements_processor_spec():
    return get_requirements_processor_spec()


def get_requirements_processor_spec():
    return _get_provisioned_spec(
        GET_REQUIREMENTS_PROCESSOR_KEY,
        "requirement_analysis.processor_spec",
    )


def _backend_processor_spec(spec: Any) -> Any:
    if getattr(spec, "key", "") != GET_REQUIREMENTS_PROCESSOR_KEY:
        return spec
    if getattr(spec, "dispatch", "") == "in_process":
        return spec
    backend_spec = copy(spec)
    backend_spec.dispatch = "in_process"
    return backend_spec


def processor_delegation_id(request_id: str) -> str:
    return extension_jobs.delegation_id(
        "requirements",
        "processed",
        request_id,
        GET_REQUIREMENTS_PROCESSOR_KEY,
    )


def parse_processed_requirements_from_dispatch_result(
    *,
    request_id: str,
    payload: dict[str, Any],
    dispatch_result: dict[str, Any],
) -> dict[str, Any] | None:
    if not request_id or not isinstance(payload, dict) or not isinstance(dispatch_result, dict):
        return None
    from provisioning.dispatch import extract_fork_text

    text = extract_fork_text(dispatch_result)
    if not text:
        return None
    return {"text": text}


def recover_processed_requirements_from_delegation(
    *,
    request_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not request_id or not isinstance(payload, dict):
        return None
    delegation_id = str(payload.get("delegation_id") or "").strip()
    if not delegation_id:
        delegation_id = processor_delegation_id(request_id)
    from provisioning.dispatch import recover_delegation_result

    result = recover_delegation_result(delegation_id)
    if result is None:
        return None
    return parse_processed_requirements_from_dispatch_result(
        request_id=request_id,
        payload={**payload, "delegation_id": delegation_id},
        dispatch_result=result,
    )


GET_REQUIREMENTS_PROCESSOR_SPEC = _ProvisionedSpecHandle(
    GET_REQUIREMENTS_PROCESSOR_KEY,
    "requirement_analysis.processor_spec",
)


def _processor_search_hints(query: str) -> list[str]:
    normalized = (query or "").lower()
    if not any(term in normalized for term in ("delayed", "confirmation", "confirms", "proposal", "adopts")):
        return []
    if not any(term in normalized for term in ("assistant", "transcript", "requirement")):
        return []
    return [
        "lag between assistant proposition and user confirmation",
        "assistant proposition",
        "user confirmation",
        "user's confirmation",
        "assistant defines requirements",
        "user confirms requirements",
        "non-user transcript rows",
    ]


def _processor_tool_unavailable(text: str) -> bool:
    return bool(_processor_tool_unavailable_reason(text))


def _processor_tool_unavailable_reason(text: str) -> str:
    lower = (text or "").lower()
    tool_names = (
        "search_requirement_units_rg",
        "search_requirement_units_fts",
        "search_requirement_units_vector",
        "query_provider_native_transcript_index",
    )
    tool_failure_markers = (
        "not available",
        "unavailable",
        "not in my session toolset",
        "not bound",
        "timed out",
        "timeout",
        "failed",
        "failure",
        "error",
        "cannot",
        "could not",
        "not working",
        "mcp server is down",
    )
    for tool_name in tool_names:
        if tool_name in lower and any(marker in lower for marker in tool_failure_markers):
            return f"{tool_name} unavailable or not working"
    generic_markers = (
        "not bound to this processor turn",
        "no mcp servers connected",
        "mcp server is down",
        "tool-availability failure",
        "cannot run the ripgrep lookup",
        "cannot execute the lookup",
        "cannot perform the lookup",
        "internal search timed out",
        "internal lookup timed out",
        "both calls timed out",
        "consecutive timeouts",
        "caller should retry",
        "not fabricating a result",
        "no lookup could be performed",
    )
    if any(marker in lower for marker in generic_markers):
        return "required processor evidence tool unavailable or not working"
    return ""


def _requirements_package_root() -> Path:
    extension_id = extension_store.extension_id_for_mcp_replacement("get-requirements")
    if not extension_id:
        raise extension_package_loader.ExtensionPackageUnavailable("requirements extension is not installed")
    return extension_package_loader.package_root(extension_id)


def _ensure_requirements_importable() -> Path:
    extension_id = extension_store.extension_id_for_mcp_replacement("get-requirements")
    if not extension_id:
        raise extension_package_loader.ExtensionPackageUnavailable("requirements extension is not installed")
    return extension_package_loader.ensure_package_importable(
        extension_id,
        "requirement_analysis",
    )


def get_processed_requirements(
    *,
    query: str,
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    normalized_query = (query or "").strip()
    if not normalized_query:
        return {
            "success": False,
            "error": "query is required",
            "requirements": [],
            "count": 0,
        }
    prepare_requirements_local_read_context()
    processed = _run_requirements_processor(
        query=normalized_query,
        cwd=cwd,
        cwds=cwds,
        all_projects=all_projects,
    )
    return build_processed_requirements_response(
        query=normalized_query,
        cwd=cwd,
        cwds=cwds,
        all_projects=all_projects,
        processed=processed,
    )


def build_processed_requirements_response(
    *,
    query: str,
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    processed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    processed = processed or {"text": "", "error": "processor_failed"}
    text = processed.get("text") if isinstance(processed, dict) else ""
    if not isinstance(text, str):
        text = ""
    error = processed.get("error") if isinstance(processed, dict) else "processor_failed"
    response = {
        "success": not bool(error),
        "text": text,
    }
    if error:
        response["error"] = error
    return response


def processor_failure_result(exc: Exception) -> dict[str, Any]:
    return {"text": "", "error": _processor_failure_message(exc)}


def _run_requirements_processor(
    *,
    query: str,
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    debug_request_id: str = "",
    delegation_id: str = "",
) -> dict[str, Any]:
    ctx = {
        "cwd": cwd,
        "cwds": cwds or [],
        "all_projects": all_projects,
    }
    if debug_request_id:
        ctx["_debug_request_id"] = debug_request_id
    if delegation_id:
        ctx["client_delegation_id"] = delegation_id
    try:
        spec = _backend_processor_spec(get_requirements_processor_spec())
    except Exception as exc:
        if debug_request_id:
            logger.warning(
                "requirements_processor_spec_unavailable request_id=%s error_type=%s",
                debug_request_id,
                type(exc).__name__,
            )
        return processor_failure_result(exc)
    tool_unavailable_retried = False
    for _attempt in range(PROCESSOR_ATTEMPTS):
        try:
            result = provisioning.run_sync(spec, query, ctx)
        except Exception as exc:
            recovered = recover_processed_requirements_from_delegation(
                request_id=debug_request_id,
                payload={
                    "query": query,
                    "cwd": cwd,
                    "cwds": cwds or [],
                    "all_projects": all_projects,
                    "delegation_id": ctx.get("client_delegation_id"),
                },
            )
            if recovered is not None:
                return recovered
            if debug_request_id:
                logger.warning(
                    "requirements_processor_failed request_id=%s error_type=%s",
                    debug_request_id,
                    type(exc).__name__,
                )
            return processor_failure_result(exc)
        text = str(getattr(result, "text", "") or "")
        if debug_request_id:
            logger.info(
                "requirements_processor_parsed request_id=%s base_session_id=%s "
                "caller_session_id=%s provider_session_id=%s chars=%s",
                debug_request_id,
                getattr(result, "base_session_id", ""),
                getattr(result, "caller_session_id", ""),
                _dispatch_provider_session_id(getattr(result, "dispatch_result", {})),
                len(text),
            )
        # The fork's evidence tools can be transiently unavailable right after a
        # backend restart (MCP not yet reconnected). One extra dispatch, only when
        # the fork reports a tool failure AND the backend's own unit search works;
        # a backend-side breakage hands the text back as-is.
        if (
            not tool_unavailable_retried
            and _attempt + 1 < PROCESSOR_ATTEMPTS
            and _processor_tool_unavailable(text)
            and _local_unit_search_healthy()
        ):
            tool_unavailable_retried = True
            if debug_request_id:
                logger.warning(
                    "requirements_processor_tool_unavailable_retry request_id=%s",
                    debug_request_id,
                )
            continue
        return {"text": text}
    return {"text": ""}


def _local_unit_search_healthy() -> bool:
    try:
        result = search_requirement_units_fts(query="requirement", all_projects=True)
    except Exception:
        return False
    return bool(result.get("success"))


def _dispatch_provider_session_id(dispatch_result: Any) -> str:
    if not isinstance(dispatch_result, dict):
        return ""
    for key in ("session_id", "provider_session_id", "agent_session_id", "fork_agent_sid"):
        value = dispatch_result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


_RATE_LIMIT_MARKERS = (
    "429",
    "rate_limit",
    "rate limit reached",
    "rate limit exceeded",
    "rate-limit reached",
    "rate-limit exceeded",
    "ratelimit",
    "too many requests",
    "resource_exhausted",
    "quota exceeded",
)


def _processor_failure_message(exc: Exception) -> str:
    error_text = str(exc).strip()
    lower = error_text.lower()
    type_name = type(exc).__name__
    lower_type = type_name.lower()
    if _is_explicit_rate_limit_error(lower):
        return (
            "processor_failed: get-requirements processor hit a provider rate limit; "
            "no retry attempted"
        )
    from requirements_query_runner import (
        RequirementsAdmissionTimeout,
        RequirementsProviderTimeout,
    )
    if isinstance(exc, RequirementsAdmissionTimeout):
        return "admission_timeout: no get-requirements processor worker was available"
    if isinstance(exc, RequirementsProviderTimeout):
        return "provider_timeout: get-requirements processor did not return requirements"
    if (
        isinstance(exc, TimeoutError)
        or "timed out" in lower
        or "timeout" in lower
        or "timeout" in lower_type
    ):
        return (
            "processor_failed: get-requirements processor timed out before returning requirements; "
            "no retry attempted"
        )
    suffix = f": {error_text}" if error_text else ""
    return f"processor_failed: {type_name}{suffix}"


def _is_explicit_rate_limit_error(lower_error_text: str) -> bool:
    return any(marker in lower_error_text for marker in _RATE_LIMIT_MARKERS)


def _sort_matches_by_ts_asc(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order matches oldest-first by timestamp so the requirement-building LLM
    reads a requirement's evolution over time; matches without a timestamp sort
    last."""
    return sorted(matches, key=lambda m: (not m.get("ts"), m.get("ts") or ""))


def search_requirements(
    *,
    rg_args: list[str] | None = None,
    query: str = "",
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    fields: list[str] | None = None,
    include_all_fields: bool = False,
    include_unprocessed_prompts: bool = False,
    provider_native_only: bool = True,
    compare: bool = False,
    max_matches: int | None = None,
) -> dict[str, Any]:
    _ensure_requirements_importable()
    normalized_args, query_error = _search_rg_args(rg_args=rg_args, query=query)
    if query_error:
        return {
            "success": False,
            "error": query_error,
            "matches": [],
            "count": 0,
            "rg_args": [],
        }
    if not normalized_args:
        return {
            "success": False,
            "error": "rg_args are required",
            "matches": [],
            "count": 0,
            "rg_args": [],
        }
    validation_error = _validate_rg_args(normalized_args)
    if validation_error:
        return {
            "success": False,
            "error": validation_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
        }
    normalized_cwds, cwds_error = _normalize_cwd_filters(cwd, cwds, all_projects=all_projects)
    if cwds_error:
        return {
            "success": False,
            "error": cwds_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
        }
    normalized_fields, fields_error = _normalize_match_fields(fields, include_all_fields=include_all_fields)
    if fields_error:
        return {
            "success": False,
            "error": fields_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
            "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
            "cwd_filters": list(normalized_cwds),
            "all_projects": all_projects,
        }
    normalized_max_matches, max_matches_error = _normalize_max_matches(max_matches)
    if max_matches_error:
        return {
            "success": False,
            "error": max_matches_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
            "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
            "cwd_filters": list(normalized_cwds),
            "all_projects": all_projects,
        }

    native_result: dict[str, Any] | None = None
    if provider_native_only or compare:
        native_result = _search_provider_native_requirements(
            rg_args=normalized_args,
            cwds=normalized_cwds,
            fields=normalized_fields,
            max_matches=normalized_max_matches,
            all_projects=all_projects,
        )
        if not compare:
            return native_result

    preparation = prepare_requirements_local_read_context()
    legacy_result = _search_requirements_prepared(
        rg_args=normalized_args,
        cwd=cwd,
        cwds=cwds,
        all_projects=all_projects,
        fields=fields,
        include_all_fields=include_all_fields,
        include_unprocessed_prompts=include_unprocessed_prompts,
        provider_native_only=False,
        max_matches=max_matches,
        preparation=preparation,
    )
    if not compare:
        return legacy_result
    assert native_result is not None
    return _compare_search_results(native=native_result, legacy=legacy_result)


def _search_requirements_prepared(
    *,
    rg_args: list[str] | None = None,
    query: str = "",
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    fields: list[str] | None = None,
    include_all_fields: bool = False,
    include_unprocessed_prompts: bool = False,
    provider_native_only: bool = False,
    max_matches: int | None = None,
    preparation: dict[str, Any],
) -> dict[str, Any]:
    normalized_args, query_error = _search_rg_args(rg_args=rg_args, query=query)
    if query_error:
        return {
            "success": False,
            "error": query_error,
            "matches": [],
            "count": 0,
            "rg_args": [],
        }
    validation_error = _validate_rg_args(normalized_args)
    if validation_error:
        return {
            "success": False,
            "error": validation_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
        }
    normalized_cwds, cwds_error = _normalize_cwd_filters(cwd, cwds, all_projects=all_projects)
    if cwds_error:
        return {
            "success": False,
            "error": cwds_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
        }
    normalized_fields, fields_error = _normalize_match_fields(fields, include_all_fields=include_all_fields)
    if fields_error:
        return {
            "success": False,
            "error": fields_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
            "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
            "cwd_filters": list(normalized_cwds),
            "all_projects": all_projects,
        }
    normalized_max_matches, max_matches_error = _normalize_max_matches(max_matches)
    if max_matches_error:
        return {
            "success": False,
            "error": max_matches_error,
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
            "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
            "cwd_filters": list(normalized_cwds),
            "all_projects": all_projects,
        }

    if provider_native_only:
        return _search_provider_native_requirements(
            rg_args=normalized_args,
            cwds=normalized_cwds,
            fields=normalized_fields,
            max_matches=normalized_max_matches,
            all_projects=all_projects,
        )

    sync = preparation["sync"]
    from requirement_analysis.prephase import units_path

    path = units_path()
    freshness = preparation["freshness"]
    if not freshness.get("fresh") and not _can_search_stale_units(freshness) and not include_unprocessed_prompts:
        return {
            "success": False,
            "error": "requirement unit extraction could not catch up",
            "matches": [],
            "count": 0,
            "rg_args": normalized_args,
            "corpus_path": str(path),
            "sync": sync,
            "freshness": _public_freshness(freshness),
        }

    records = _filter_records_by_cwds(_load_unit_records(), normalized_cwds)
    unit_projection_path, line_records = _write_unit_projection(records)
    try:
        rg_result = _run_rg(unit_projection_path, normalized_args)
        matches = _project_records(
            _records_from_rg_stdout(rg_result["stdout"], line_records, normalized_max_matches),
            normalized_fields,
        )
    finally:
        try:
            unit_projection_path.unlink()
        except OSError:
            pass
    fallback_result = _search_unprocessed_prompts(
        rg_args=normalized_args,
        freshness=freshness,
        cwds=normalized_cwds,
        fields=normalized_fields,
        enabled=include_unprocessed_prompts,
        remaining=_remaining_matches(normalized_max_matches, len(matches)),
    )
    matches.extend(fallback_result.pop("matches"))
    native_result = _search_native_transcript_bundles(
        rg_args=normalized_args,
        cwds=normalized_cwds,
        fields=normalized_fields,
        enabled=include_unprocessed_prompts and fields is None,
        remaining=_remaining_matches(normalized_max_matches, len(matches)),
    )
    matches.extend(native_result.pop("matches"))
    matches = _sort_matches_by_ts_asc(matches)
    stdout = _records_stdout(matches)
    authoritative = bool(freshness.get("fresh"))
    return {
        "success": rg_result["returncode"] in (0, 1),
        "authoritative": authoritative,
        "authority": "fresh_requirement_units" if authoritative else "stale_requirement_units",
        "rg_args": normalized_args,
        "command": rg_result["command"],
        "returncode": rg_result["returncode"],
        "stdout": stdout,
        "stderr": rg_result["stderr"],
        "matches": matches,
        "count": len(matches),
        "unprocessed_prompt_fallback": fallback_result,
        "native_transcript_bundles": native_result,
        "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
        "cwd_filters": list(normalized_cwds),
        "all_projects": all_projects,
        "match_fields": list(normalized_fields) if normalized_fields is not None else "all",
        "max_matches": normalized_max_matches,
        "default_match_fields": list(DEFAULT_MATCH_FIELDS),
        "available_match_fields": list(MATCH_FIELD_ORDER),
        "corpus_path": str(path),
        "unit_corpus_path": str(unit_projection_path),
        "sync": sync,
        "freshness": _public_freshness(freshness),
    }



def _unit_fts_db_path() -> Path:
    from requirement_analysis.prephase import units_path

    return units_path().parent / UNIT_FTS_DB_NAME


def _unit_fts_state(path: Path) -> dict[str, str]:
    try:
        st = path.stat()
    except OSError:
        return {"exists": "0", "mtime_ns": "0", "size": "0"}
    return {"exists": "1", "mtime_ns": str(st.st_mtime_ns), "size": str(st.st_size)}


def _unit_fts_quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _unit_fts_match_expr(query: str) -> str:
    tokens = [tok.lower() for tok in UNIT_FTS_TOKEN_RE.findall(query or "")]
    deduped: list[str] = []
    for token in tokens:
        if token not in deduped:
            deduped.append(token)
    return " OR ".join(_unit_fts_quote(token) for token in deduped)


def _connect_unit_fts(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_unit_fts_index(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from requirement_analysis.prephase import units_path

    source_path = units_path()
    if not source_path.exists():
        return {"ready": False, "reason": "requirement_units_missing", "path": str(_unit_fts_db_path())}
    source_state = _unit_fts_state(source_path)
    db_path = _unit_fts_db_path()
    if records is None:
        records = _load_unit_records()
    conn = _connect_unit_fts(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS requirement_units_fts_state(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS requirement_units_fts USING fts5(
                search_text,
                record_json UNINDEXED,
                cwd UNINDEXED,
                ts UNINDEXED,
                tokenize='unicode61'
            );
        """)
        stored = {
            str(row[0]): str(row[1])
            for row in conn.execute("SELECT key, value FROM requirement_units_fts_state")
        }
        expected = {
            "source_mtime_ns": source_state["mtime_ns"],
            "source_size": source_state["size"],
            "record_count": str(len(records)),
        }
        if stored != expected:
            with conn:
                conn.execute("DELETE FROM requirement_units_fts")
                conn.execute("DELETE FROM requirement_units_fts_state")
                conn.executemany(
                    "INSERT INTO requirement_units_fts(search_text, record_json, cwd, ts) VALUES (?, ?, ?, ?)",
                    [
                        (
                            _unit_search_line(record),
                            json.dumps(record, ensure_ascii=False, sort_keys=True),
                            str(record.get("cwd") or ""),
                            str(record.get("ts") or ""),
                        )
                        for record in records
                    ],
                )
                conn.executemany(
                    "INSERT INTO requirement_units_fts_state(key, value) VALUES (?, ?)",
                    list(expected.items()),
                )
        return {
            "ready": True,
            "path": str(db_path),
            "source_path": str(source_path),
            "record_count": len(records),
            "rebuilt": stored != expected,
        }
    finally:
        conn.close()


def search_requirement_units_fts(
    *,
    query: str,
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    fields: list[str] | None = None,
    include_all_fields: bool = False,
) -> dict[str, Any]:
    _ensure_requirements_importable()
    normalized_query = (query or "").strip()
    if not normalized_query:
        return {"success": False, "error": "query is required", "matches": [], "count": 0}
    normalized_cwds, cwds_error = _normalize_cwd_filters(cwd, cwds, all_projects=all_projects)
    if cwds_error:
        return {"success": False, "error": cwds_error, "matches": [], "count": 0}
    normalized_fields, fields_error = _normalize_match_fields(fields, include_all_fields=include_all_fields)
    if fields_error:
        return {"success": False, "error": fields_error, "matches": [], "count": 0}
    records = _load_unit_records()
    index = _ensure_unit_fts_index(records)
    if not index.get("ready"):
        return {
            "success": True,
            "searched": False,
            "reason": index.get("reason") or "index_not_ready",
            "matches": [],
            "count": 0,
            "query": normalized_query,
            "index": index,
            "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
            "cwd_filters": list(normalized_cwds),
            "all_projects": all_projects,
        }
    match_expr = _unit_fts_match_expr(normalized_query)
    if not match_expr:
        return {
            "success": True,
            "searched": False,
            "reason": "no_query_terms",
            "matches": [],
            "count": 0,
            "query": normalized_query,
            "index": index,
        }
    where = "requirement_units_fts MATCH ?"
    params: list[Any] = [match_expr]
    if normalized_cwds:
        placeholders = ",".join("?" for _ in normalized_cwds)
        where += f" AND cwd IN ({placeholders})"
        params.extend(normalized_cwds)
    sql = (
        "SELECT record_json FROM requirement_units_fts "
        f"WHERE {where} "
        "ORDER BY bm25(requirement_units_fts), ts"
    )
    conn = sqlite3.connect(str(_unit_fts_db_path()))
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error as exc:
        return {
            "success": False,
            "searched": False,
            "error": f"{type(exc).__name__}: {exc}",
            "matches": [],
            "count": 0,
            "query": normalized_query,
            "index": index,
        }
    finally:
        conn.close()
    raw_matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_cwds = set(normalized_cwds)
    for (record_json,) in rows:
        try:
            record = json.loads(record_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if allowed_cwds and record.get("cwd") not in allowed_cwds:
            continue
        key = str(record.get("source_key") or json.dumps(record, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        raw_matches.append(record)
    matches = _project_records(raw_matches, normalized_fields)
    return {
        "success": True,
        "searched": True,
        "query": normalized_query,
        "match_expr": match_expr,
        "matches": matches,
        "count": len(matches),
        "index": index,
        "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
        "cwd_filters": list(normalized_cwds),
        "all_projects": all_projects,
        "match_fields": list(normalized_fields) if normalized_fields is not None else "all",
        "max_matches": None,
    }


def _default_unit_vector_embed(texts: list[str]):
    from requirement_analysis import unit_vector_embedder

    return unit_vector_embedder.embed(texts)


def _unit_vector_path() -> Path:
    from requirement_analysis.prephase import units_path

    return units_path().parent / UNIT_VECTOR_DB_NAME


def _unit_vector_state_path() -> Path:
    from requirement_analysis.prephase import units_path

    return units_path().parent / UNIT_VECTOR_STATE_NAME


# Serializes vector-index builds across threads. Concurrent processor forks
# (admission allows 2) each call _ensure_unit_vector_index; two simultaneous
# full-corpus ONNX re-embeds thrash CPU and race on the npz write. The lock
# collapses them into one build + cheap reuse.
_UNIT_VECTOR_INDEX_LOCK = threading.Lock()


def _unit_vector_text(record: dict[str, Any]) -> str:
    """Text fed to the embedder. Embeds the requirement ``text`` only — the FTS
    ``_unit_search_line`` boilerplate (cwd + enum fields) dominates short texts
    and flattened cosines to ~0.9 in the recall eval; ``text`` (+kind) is the
    honest semantic signal."""
    text = (record.get("text") or "").strip()
    kind = (record.get("kind") or "").strip()
    return f"{kind}: {text}" if kind else text


def _ensure_unit_vector_index(
    records: list[dict[str, Any]] | None = None,
    embedder=None,
) -> dict[str, Any]:
    import json as _json

    from requirement_analysis.prephase import units_path

    source_path = units_path()
    if not source_path.exists():
        return {"ready": False, "reason": "requirement_units_missing", "path": str(_unit_vector_path())}
    if embedder is None:
        embedder = _default_unit_vector_embed
    if records is None:
        records = _load_unit_records()

    source_state = _unit_fts_state(source_path)
    db_path = _unit_vector_path()
    state_path = _unit_vector_state_path()
    expected = {
        "source_mtime_ns": source_state["mtime_ns"],
        "source_size": source_state["size"],
        "record_count": str(len(records)),
    }

    def _read_state() -> dict[str, str]:
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                return {str(k): str(v) for k, v in _json.load(fh).items()}
        except (OSError, ValueError):
            return {}

    if _read_state() == expected and db_path.exists():
        return _unit_vector_index_ok(db_path, source_path, len(records), rebuilt=False)

    # Serialize builds: a second processor fork may have produced the exact
    # index while we waited, and two concurrent full-corpus re-embeds must not
    # run together. Re-check state inside the lock.
    with _UNIT_VECTOR_INDEX_LOCK:
        stored = _read_state()
        if stored == expected and db_path.exists():
            return _unit_vector_index_ok(db_path, source_path, len(records), rebuilt=False)
        vectors, source_keys, cwds, text_shas = _build_unit_vector_index(
            records, embedder=embedder, db_path=db_path
        )
        _write_unit_vector_index_atomic(db_path, vectors, source_keys, cwds, text_shas)
        with open(state_path, "w", encoding="utf-8") as fh:
            _json.dump(expected, fh)
    return _unit_vector_index_ok(db_path, source_path, len(records), rebuilt=True)


def _unit_vector_index_ok(
    db_path: Path, source_path: Path, record_count: int, *, rebuilt: bool
) -> dict[str, Any]:
    return {
        "ready": True,
        "path": str(db_path),
        "source_path": str(source_path),
        "record_count": record_count,
        "rebuilt": rebuilt,
    }


def _load_unit_vector_arrays(db_path: Path):
    """Return (vectors, source_keys, cwds, text_shas) for an existing index, or
    None when the index is missing, ill-formed, internally inconsistent, or
    predates the per-record content-hash guard (forces a clean full rebuild)."""
    import numpy as np

    if not db_path.exists():
        return None
    try:
        with np.load(db_path, allow_pickle=False) as data:
            vectors = np.asarray(data["vectors"], dtype=np.float32)
            source_keys = [str(key) for key in data["source_keys"]]
            cwds = [str(value) for value in data["cwds"]]
            if "text_shas" not in data:
                return None
            text_shas = [str(sha) for sha in data["text_shas"]]
    except (OSError, ValueError, KeyError):
        return None
    if vectors.ndim != 2:
        return None
    if not (vectors.shape[0] == len(source_keys) == len(cwds) == len(text_shas)):
        return None
    return vectors, source_keys, cwds, text_shas


def _unit_vector_text_sha(record: dict[str, Any]) -> str:
    return hashlib.sha256(_unit_vector_text(record).encode("utf-8")).hexdigest()


def _build_unit_vector_index(
    records: list[dict[str, Any]],
    *,
    embedder,
    db_path: Path,
):
    """Return (vectors, source_keys, cwds, text_shas) covering ``records``.

    MiniLM embeddings are deterministic per text, so an existing index's
    vectors are reusable for any record whose embedded text is unchanged.
    Incremental embedding reuses the cached prefix and embeds only the
    appended tail — guarded by BOTH source_key AND a per-record sha256 of the
    embedded text. The text guard is required because source_key is structural
    (``f"{source_prompt_key}:unit:{unit_index}"``, prephase.py), not
    content-derived: ``_replace_units_for_prompt_keys`` can re-append a
    re-extracted unit under the same source_key with different text, which
    key-only matching would silently serve as stale vectors. Fall back to a
    full re-embed on cold start, any prefix mismatch, or embedding-dim change.
    Re-embedding the whole corpus on every search wastes ~57s of CPU per call
    (and under concurrent forks, tens of minutes), which is what pushed
    processor runs past their dispatch budget into ReadTimeout.
    """
    import numpy as np

    new_keys = [str(r.get("source_key") or "") for r in records]
    new_cwds = [str(r.get("cwd") or "") for r in records]
    new_shas = [_unit_vector_text_sha(r) for r in records]
    keys_arr = np.asarray(new_keys)
    cwds_arr = np.asarray(new_cwds)
    shas_arr = np.asarray(new_shas)

    existing = _load_unit_vector_arrays(db_path)
    if existing is not None:
        ex_vectors, ex_keys, _ex_cwds, ex_shas = existing
        prefix_len = ex_vectors.shape[0]
        prefix_intact = (
            0 < prefix_len <= len(records)
            and ex_keys == new_keys[:prefix_len]
            and ex_shas == new_shas[:prefix_len]
        )
        if prefix_intact:
            tail = records[prefix_len:]
            if not tail:
                return ex_vectors, keys_arr, cwds_arr, shas_arr
            tail_vectors = np.asarray(
                embedder([_unit_vector_text(r) for r in tail]),
                dtype=np.float32,
            )
            if tail_vectors.ndim == 2 and tail_vectors.shape[1] == ex_vectors.shape[1]:
                vectors = np.vstack([ex_vectors, tail_vectors]).astype(np.float32, copy=False)
                return vectors, keys_arr, cwds_arr, shas_arr

    # Cold start, prefix/key/text mismatch, or embedding-dim change: full re-embed.
    text_lines = [_unit_vector_text(record) for record in records]
    vectors = embedder(text_lines) if text_lines else np.zeros((0, 1), dtype=np.float32)
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim != 2:
        vectors = vectors.reshape(0, 1)
    return vectors, keys_arr, cwds_arr, shas_arr


def _write_unit_vector_index_atomic(
    db_path: Path, vectors, source_keys, cwds, text_shas
) -> None:
    """Write the index via a staged temp file + os.replace, so a concurrent
    np.load reader (another processor fork mid-search) never observes a
    half-written file. tempfile suffix must be .npz or numpy appends one and
    os.replace misses the target."""
    import numpy as np

    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".unit_vectors.", suffix=".npz", dir=str(db_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        np.savez(
            tmp_path,
            vectors=vectors,
            source_keys=np.asarray([str(k) for k in source_keys]),
            cwds=np.asarray([str(c) for c in cwds]),
            text_shas=np.asarray([str(s) for s in text_shas]),
        )
        os.replace(tmp_path, db_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def search_requirement_units_vector(
    *,
    query: str,
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    fields: list[str] | None = None,
    include_all_fields: bool = False,
    embedder=None,
) -> dict[str, Any]:
    """Semantic (vector) search over extracted requirement_units.jsonl via ONNX
    MiniLM cosine similarity. Mirrors ``search_requirement_units_fts`` shape.
    Closes the BM25-blind slice — requirements semantically related to the query
    but sharing no tokens with it. The processor fuses this with rg + FTS +
    transcript-SQL results."""
    import numpy as np

    _ensure_requirements_importable()
    normalized_query = (query or "").strip()
    if not normalized_query:
        return {"success": False, "error": "query is required", "matches": [], "count": 0}
    normalized_cwds, cwds_error = _normalize_cwd_filters(cwd, cwds, all_projects=all_projects)
    if cwds_error:
        return {"success": False, "error": cwds_error, "matches": [], "count": 0}
    normalized_fields, fields_error = _normalize_match_fields(fields, include_all_fields=include_all_fields)
    if fields_error:
        return {"success": False, "error": fields_error, "matches": [], "count": 0}
    records = _load_unit_records()
    index = _ensure_unit_vector_index(records, embedder=embedder)
    if not index.get("ready"):
        return {
            "success": True,
            "searched": False,
            "reason": index.get("reason") or "index_not_ready",
            "matches": [],
            "count": 0,
            "query": normalized_query,
            "index": index,
            "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
            "cwd_filters": list(normalized_cwds),
            "all_projects": all_projects,
        }
    if embedder is None:
        embedder = _default_unit_vector_embed

    db_path = _unit_vector_path()
    with np.load(db_path, allow_pickle=False) as data:
        vectors = np.asarray(data["vectors"], dtype=np.float32)
        source_keys = [str(key) for key in data["source_keys"]]
        index_cwds = [str(value) for value in data["cwds"]]
    if vectors.shape[0] == 0:
        return {
            "success": True,
            "searched": True,
            "query": normalized_query,
            "matches": [],
            "count": 0,
            "index": index,
            "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
            "cwd_filters": list(normalized_cwds),
            "all_projects": all_projects,
            "match_fields": list(normalized_fields) if normalized_fields is not None else "all",
            "max_matches": None,
        }

    query_vec = np.asarray(embedder([normalized_query]), dtype=np.float32).reshape(-1)
    dim = vectors.shape[1]
    if query_vec.shape[0] != dim:
        return {
            "success": False,
            "searched": False,
            "error": f"embedding_dim_mismatch: query={query_vec.shape[0]} index={dim}",
            "matches": [],
            "count": 0,
            "query": normalized_query,
            "index": index,
        }
    scores = vectors @ query_vec

    allowed_cwds = set(normalized_cwds)
    scored: list[tuple[float, int]] = []
    for position, score in enumerate(scores):
        if allowed_cwds and index_cwds[position] not in allowed_cwds:
            continue
        scored.append((float(score), position))
    scored.sort(key=lambda item: item[0], reverse=True)

    # records may carry duplicates by source_key; keep the first occurrence
    records_by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("source_key") or json.dumps(record, sort_keys=True))
        records_by_key.setdefault(key, record)

    raw_matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for score, position in scored:
        if score <= 0.0:
            break
        key = source_keys[position] if position < len(source_keys) else ""
        record = records_by_key.get(key)
        if record is None:
            continue
        dedupe = key or json.dumps(record, sort_keys=True)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        scored_record = dict(record)
        scored_record["vector_score"] = score
        raw_matches.append(scored_record)

    matches = _project_records(raw_matches, normalized_fields)
    return {
        "success": True,
        "searched": True,
        "query": normalized_query,
        "matches": matches,
        "count": len(matches),
        "index": index,
        "cwd_filter": normalized_cwds[0] if len(normalized_cwds) == 1 else "",
        "cwd_filters": list(normalized_cwds),
        "all_projects": all_projects,
        "match_fields": list(normalized_fields) if normalized_fields is not None else "all",
        "max_matches": None,
    }


def run_native_index_sql(sql: str) -> dict[str, Any]:
    """Free-form read-only SQL on the native transcript FTS index for the
    requirements processor. Safety lives in run_readonly_sql (SELECT-only
    authorizer, mode=ro, deadline); this wrapper adds the same cold-cache
    interrupt retry as the bundle search.

    Successful raw transcript reads are also the deterministic hand-off point
    for on-demand extraction: every returned row with path+element_index is
    mapped to a transcript window, queued durably, then a detached background
    worker is nudged to mine those windows into requirement_units.jsonl.
    """
    import native_transcript_index

    # Full-recall processor queries can legitimately take longer than the
    # interactive default deadline; run them under the generous processor budget
    # instead of trimming or row-capping. A cold page cache can still trip the
    # deadline on the first FTS scan, so retry the warm run under at least the
    # same budget.
    result = native_transcript_index.run_readonly_sql(
        sql,
        timeout_s=NATIVE_INDEX_SQL_TIMEOUT_SECONDS,
    )
    if "interrupted" in str(result.get("error") or ""):
        result = native_transcript_index.run_readonly_sql(
            sql,
            timeout_s=max(
                NATIVE_INDEX_SQL_TIMEOUT_SECONDS,
                NATIVE_BUNDLE_COLD_RETRY_TIMEOUT_SECONDS,
            ),
        )
    error = result.get("error")
    response = {"success": not bool(error), **result}
    if not error:
        try:
            _ensure_requirements_importable()
            from requirement_analysis import on_demand

            visited = on_demand.record_visited_windows_from_sql_result(sql, result)
            response["visited_windows"] = visited
            if int(visited.get("recorded") or 0) > 0:
                response["on_demand_extraction"] = _ensure_on_demand_background_extraction()
        except Exception as exc:
            response["visited_windows"] = {"recorded": 0, "error": str(exc)}
    return response


def _search_provider_native_requirements(
    *,
    rg_args: list[str],
    cwds: tuple[str, ...],
    fields: tuple[str, ...] | None,
    max_matches: int | None,
    all_projects: bool,
) -> dict[str, Any]:
    native_result = _search_native_transcript_bundles(
        rg_args=rg_args,
        cwds=cwds,
        fields=fields,
        enabled=True,
        remaining=max_matches,
    )
    matches = _sort_matches_by_ts_asc(native_result.get("matches", []))
    native_result = {**native_result, "matches": matches, "count": len(matches)}
    return {
        "success": not bool(native_result.get("error")),
        "authoritative": True,
        "authority": "provider_native_transcript_corpus",
        "rg_args": rg_args,
        "command": [],
        "returncode": 0 if not native_result.get("error") else 1,
        "stdout": _records_stdout(matches),
        "stderr": native_result.get("error", ""),
        "matches": matches,
        "count": len(matches),
        "unprocessed_prompt_fallback": {"enabled": False, "searched": False, "matches": [], "count": 0},
        "native_transcript_bundles": native_result,
        "cwd_filter": cwds[0] if len(cwds) == 1 else "",
        "cwd_filters": list(cwds),
        "all_projects": all_projects,
        "match_fields": list(fields) if fields is not None else "all",
        "max_matches": max_matches,
        "default_match_fields": list(DEFAULT_MATCH_FIELDS),
        "available_match_fields": list(MATCH_FIELD_ORDER),
        "corpus_path": "provider_native_transcript_index",
        "sync": {"success": True, "changed": False, "skipped": "provider_native_only"},
        "freshness": {"fresh": True, "skipped": "provider_native_only"},
    }


def _compare_match_keys(matches: list[dict[str, Any]]) -> set[str]:
    return {
        hashlib.sha256(
            json.dumps(match, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for match in matches
    }


def _compare_search_results(
    *,
    native: dict[str, Any],
    legacy: dict[str, Any],
) -> dict[str, Any]:
    """Manual comparison mode: run the provider-native and legacy mined-units
    paths on the same query and report where they diverge. Diagnostic only —
    used to decide when the legacy pipeline can be sunset."""
    native_matches = native.get("matches") or []
    legacy_matches = legacy.get("matches") or []
    native_keys = _compare_match_keys(native_matches)
    legacy_keys = _compare_match_keys(legacy_matches)
    native_ts = {m.get("ts") for m in native_matches if m.get("ts")}
    legacy_ts = {m.get("ts") for m in legacy_matches if m.get("ts")}
    return {
        "success": bool(native.get("success")) and bool(legacy.get("success")),
        "compare": True,
        "native": native,
        "legacy": legacy,
        "diff": {
            "native_count": len(native_matches),
            "legacy_count": len(legacy_matches),
            "identical_match_count": len(native_keys & legacy_keys),
            "native_only_ts": sorted(native_ts - legacy_ts),
            "legacy_only_ts": sorted(legacy_ts - native_ts),
        },
    }


def prepare_requirements_context(
    *,
    allowed_unhandled_prompts: int = 1,
) -> dict[str, Any]:
    _ensure_requirements_importable()
    sync = _refresh_user_prompts()
    freshness = _requirement_unit_freshness(allowed_unhandled_prompts=allowed_unhandled_prompts)
    extraction = _ensure_background_extraction()
    return {
        "success": bool(sync.get("success")) and bool(freshness.get("fresh")),
        "sync": sync,
        "freshness": freshness,
        "extraction": extraction,
    }


def prepare_requirements_local_read_context(
    *,
    allowed_unhandled_prompts: int = 1,
) -> dict[str, Any]:
    _ensure_requirements_importable()
    freshness = _requirement_unit_freshness(allowed_unhandled_prompts=allowed_unhandled_prompts)
    extraction = _ensure_background_extraction()
    return {
        "success": bool(freshness.get("fresh")),
        "sync": {"success": True, "changed": False, "skipped": "local_read"},
        "freshness": freshness,
        "extraction": extraction,
    }


def prewarm_requirements_read_model() -> None:
    _requirement_unit_freshness(allowed_unhandled_prompts=1)


def _launch_requirements_background(args: list[str]) -> dict[str, Any]:
    from requirement_analysis import cli

    backend_dir = Path(__file__).resolve().parent
    return cli.launch_background(
        args,
        extra_pythonpath=[str(_requirements_package_root()), str(backend_dir)],
        cwd=str(backend_dir),
    )


def _ensure_on_demand_background_extraction() -> dict[str, Any]:
    """Best-effort nudge for the on-demand visited-window miner."""
    try:
        return _launch_requirements_background(["--extract-on-demand", "--background"])
    except RuntimeError as exc:
        return {"running": True, "detail": str(exc)}
    except Exception as exc:
        return {"running": False, "error": str(exc)}


def _ensure_background_extraction() -> dict[str, Any]:
    """Ensure the detached requirement-extraction runner is alive. Spawns the
    CLI ``--extract --background`` process, which owns unit extraction + the
    downstream DAG to completion and writes run_status under its OWN pid (so a
    dead run is detectable, unlike an in-backend thread under the always-alive
    backend pid). Best-effort and non-blocking: a run already in flight is a
    no-op; any failure is swallowed so the query path never blocks on it.

    The detached child's interpreter never had the requirements package or the
    backend modules on sys.path, so inject both roots into its PYTHONPATH."""
    try:
        return _launch_requirements_background(["--extract", "--background"])
    except RuntimeError as exc:
        # "already running" guard from launch_background — a runner is in
        # flight. Expected steady state, not an error.
        return {"running": True, "detail": str(exc)}
    except Exception as exc:
        return {"running": False, "error": str(exc)}


def _can_search_stale_units(freshness: dict[str, Any]) -> bool:
    unit_sync = freshness.get("unit_sync")
    if not isinstance(unit_sync, dict):
        return False
    error = unit_sync.get("error")
    return isinstance(error, str) and "requirement unit extraction already running" in error


def _refresh_user_prompts() -> dict[str, Any]:
    try:
        from requirement_analysis.corpus import user_prompts_path

        sync_user_prompts = _load_sync_user_prompts()
        before = _safe_mtime(user_prompts_path())
        sync_user_prompts.sync()
        after = _safe_mtime(user_prompts_path())
        return {"success": True, "changed": before != after}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _load_sync_user_prompts() -> Any:
    script = _requirements_package_root() / "scripts" / "sync_user_prompts.py"
    spec = importlib.util.spec_from_file_location("bc_sync_user_prompts", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load sync_user_prompts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _requirement_unit_freshness(*, allowed_unhandled_prompts: int = 1) -> dict[str, Any]:
    try:
        from requirement_analysis.prephase import unit_freshness

        return unit_freshness(allowed_unhandled_prompts=allowed_unhandled_prompts)
    except Exception as exc:
        return {"success": False, "fresh": False, "error": str(exc)}


def _run_rg(path: Path, rg_args: list[str]) -> dict[str, Any]:
    rg = shutil.which("rg")
    if not rg:
        return {
            "command": ["rg", *rg_args, str(path)],
            "returncode": 127,
            "stdout": "",
            "stderr": "rg executable not found",
        }
    command = [rg, *rg_args, str(path)]
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": f"rg timed out after {RG_TIMEOUT_SECONDS}s",
        }
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _load_unit_records() -> list[dict[str, Any]]:
    from requirement_analysis.prephase import load_units

    return [
        unit
        for unit in load_units()
        if isinstance(unit.get("text"), str) and unit.get("text", "").strip()
    ]


def _search_unprocessed_prompts(
    *,
    rg_args: list[str],
    freshness: dict[str, Any],
    cwds: tuple[str, ...],
    fields: tuple[str, ...] | None,
    enabled: bool,
    remaining: int | None,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "searched": False, "matches": [], "count": 0}
    if remaining is not None and remaining <= 0:
        return {"enabled": True, "searched": False, "matches": [], "count": 0, "reason": "unit_match_limit_reached"}
    records = _filter_records_by_cwds(_load_unprocessed_prompt_records(freshness), cwds)
    if not records:
        return {"enabled": True, "searched": False, "matches": [], "count": 0, "reason": "no_unprocessed_prompts"}
    projection_path, line_records = _write_prompt_fallback_projection(records)
    try:
        rg_result = _run_rg(projection_path, rg_args)
        raw_matches = _records_from_prompt_rg_stdout(rg_result["stdout"], line_records, remaining)
        matches = _project_records(raw_matches, fields)
    finally:
        try:
            projection_path.unlink()
        except OSError:
            pass
    return {
        "enabled": True,
        "searched": True,
        "matches": matches,
        "count": len(matches),
        "command": rg_result["command"],
        "returncode": rg_result["returncode"],
        "stderr": rg_result["stderr"],
        "candidate_count": len(records),
    }


def _load_unprocessed_prompt_records(freshness: dict[str, Any]) -> list[dict[str, Any]]:
    from requirement_analysis.prompts import prompt_key

    keys = freshness.get("unhandled_prompt_keys")
    if not isinstance(keys, list):
        return []
    unhandled_keys = {key for key in keys if isinstance(key, str)}
    if not unhandled_keys:
        return []
    projected = freshness.get("_unhandled_prompt_records")
    if isinstance(projected, (list, tuple)):
        prompts = projected
    else:
        from requirement_analysis.prompts import load_prompts

        prompts = load_prompts()
    records: list[dict[str, Any]] = []
    for prompt in prompts:
        key = prompt.get("key") or prompt_key(prompt)
        if key not in unhandled_keys:
            continue
        records.append(_prompt_fallback_record(prompt, key))
    return records


def _public_freshness(freshness: dict[str, Any]) -> dict[str, Any]:
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in freshness.items()
        if not key.startswith("_")
    }


def _prompt_fallback_record(prompt: dict[str, Any], key: str) -> dict[str, Any]:
    text = prompt.get("text") if isinstance(prompt.get("text"), str) else ""
    return {
        "source_key": f"{key}:unprocessed-prompt",
        "source_prompt_key": key,
        "unit_index": None,
        "text": text,
        "kind": PROMPT_FALLBACK_KIND,
        "polarity": "",
        "strength": "medium",
        "sid": prompt.get("sid"),
        "ts": prompt.get("ts"),
        "user_seq": prompt.get("user_seq"),
        "source": prompt.get("source") or "user",
        "source_text": text,
        "prev_reply": prompt.get("prev_reply") if isinstance(prompt.get("prev_reply"), str) else "",
        "cwd": prompt.get("cwd") if isinstance(prompt.get("cwd"), str) else "",
        "edited_files": prompt.get("edited_files") if isinstance(prompt.get("edited_files"), list) else [],
        "git_commits": prompt.get("git_commits") if isinstance(prompt.get("git_commits"), list) else [],
    }


def _search_native_transcript_bundles(
    *,
    rg_args: list[str],
    cwds: tuple[str, ...],
    fields: tuple[str, ...] | None,
    enabled: bool,
    remaining: int | None,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "searched": False, "matches": [], "count": 0}
    if remaining is not None and remaining <= 0:
        return {"enabled": True, "searched": False, "matches": [], "count": 0, "reason": "match_limit_reached"}
    query = _query_text_from_rg_args(rg_args)
    if not query:
        return {"enabled": True, "searched": False, "matches": [], "count": 0, "reason": "no_query_terms"}
    native_limit = min(remaining, NATIVE_BUNDLE_HIT_LIMIT) if remaining is not None else NATIVE_BUNDLE_HIT_LIMIT
    raw = _native_transcript_bundle_records(query=query, cwds=cwds, limit=native_limit)
    matches = _project_records(raw["matches"], fields)
    return {
        "enabled": True,
        "searched": raw["searched"],
        "matches": matches,
        "count": len(matches),
        "query": query,
        "index": raw["index"],
        **({"error": raw["error"]} if raw.get("error") else {}),
        **({"reason": raw["reason"]} if raw.get("reason") else {}),
    }


def _query_text_from_rg_args(rg_args: list[str]) -> str:
    patterns: list[str] = []
    skip_next = False
    for index, arg in enumerate(rg_args):
        if skip_next:
            skip_next = False
            continue
        if arg in ("-e", "--regexp"):
            if index + 1 < len(rg_args):
                patterns.append(rg_args[index + 1])
                skip_next = True
            continue
        if arg in RG_OPTIONS_WITH_VALUE:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        patterns.append(arg)
    return " ".join(pattern.strip() for pattern in patterns if pattern.strip())


def _native_transcript_bundle_records(
    *,
    query: str,
    cwds: tuple[str, ...],
    limit: int,
) -> dict[str, Any]:
    try:
        from native_session_prompt_search import _query_tokens
        import native_transcript_index

        tokens = _query_tokens(query)
        if not tokens:
            return _native_bundle_result([], searched=False, reason="no_tokens")
        index_state = native_transcript_index.ensure_fresh_for_read()
        if not index_state["usable"]:
            return _native_bundle_result([], searched=False, reason="index_not_usable", index=index_state)
        rows = _native_transcript_sql_window_rows(
            native_transcript_index,
            tokens=tokens,
            cwds=cwds,
            limit=limit,
        )
        return _native_bundle_result(_native_bundle_records_from_rows(rows), searched=True, index=index_state)
    except Exception as exc:
        return _native_bundle_result([], searched=False, error=str(exc))


def _native_bundle_result(
    matches: list[dict[str, Any]],
    *,
    searched: bool,
    index: dict[str, Any] | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "matches": matches,
        "searched": searched,
        "index": index or {"covered": False, "usable": False},
        **({"reason": reason} if reason else {}),
        **({"error": error} if error else {}),
    }


def _native_transcript_sql_window_rows(
    native_transcript_index: Any,
    *,
    tokens: list[str],
    cwds: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    match_expr = native_transcript_index._match_expr(tokens)
    cwd_clause = ""
    params: list[Any] = [match_expr]
    if cwds:
        placeholders = ",".join("?" for _ in cwds)
        cwd_clause = f" AND cwd IN ({placeholders})"
        params.extend(cwds)
    params.extend([
        limit,
        NATIVE_BUNDLE_WINDOW_BEFORE,
        NATIVE_BUNDLE_WINDOW_AFTER,
    ])
    sql = f"""
        WITH hits AS (
            SELECT
                path,
                CAST(element_index AS INTEGER) AS hit_index,
                bm25(native_element_fts) AS rank
            FROM native_element_fts
            WHERE native_element_fts MATCH ?{cwd_clause}
            ORDER BY rank, path, hit_index
            LIMIT ?
        ),
        windows AS (
            SELECT
                path,
                hit_index,
                rank,
                hit_index - ? AS start_index,
                hit_index + ? AS end_index
            FROM hits
        ),
        ordered_windows AS (
            SELECT
                path,
                hit_index,
                rank,
                start_index,
                end_index,
                MAX(end_index) OVER (
                    PARTITION BY path
                    ORDER BY start_index, end_index
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS previous_end_index
            FROM windows
        ),
        marked_windows AS (
            SELECT
                path,
                hit_index,
                rank,
                start_index,
                end_index,
                CASE
                    WHEN previous_end_index IS NULL OR start_index > previous_end_index THEN 1
                    ELSE 0
                END AS starts_new_window
            FROM ordered_windows
        ),
        grouped_windows AS (
            SELECT
                path,
                hit_index,
                rank,
                start_index,
                end_index,
                SUM(starts_new_window) OVER (
                    PARTITION BY path
                    ORDER BY start_index, end_index
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS window_group
            FROM marked_windows
        ),
        merged_window_bounds AS (
            SELECT
                path,
                window_group,
                MIN(rank) AS rank,
                MIN(start_index) AS start_index,
                MAX(end_index) AS end_index
            FROM grouped_windows
            GROUP BY path, window_group
        ),
        ranked_window_hits AS (
            SELECT
                path,
                window_group,
                hit_index,
                ROW_NUMBER() OVER (
                    PARTITION BY path, window_group
                    ORDER BY rank, hit_index
                ) AS hit_order
            FROM grouped_windows
        ),
        merged_windows AS (
            SELECT
                b.path,
                h.hit_index,
                b.rank,
                b.start_index,
                b.end_index
            FROM merged_window_bounds b
            JOIN ranked_window_hits h
                ON h.path = b.path
                AND h.window_group = b.window_group
                AND h.hit_order = 1
        )
        SELECT
            w.hit_index,
            e.text,
            e.path,
            e.sid,
            e.cwd,
            e.tag,
            e.element_kind,
            e.tool_name,
            e.ts_utc,
            e.role,
            e.element_id,
            e.element_index,
            e.text_sha256,
            e.norm_text_sha256,
            e.prefix_1024_sha256,
            e.prefix_4096_sha256,
            e.prefix_8192_sha256,
            e.text_len,
            e.norm_text_len,
            rb.group_id AS repeat_group_id,
            rb.raw_tail_start AS repeat_raw_tail_start,
            rb.norm_tail_start AS repeat_norm_tail_start,
            rg.kind AS repeat_kind,
            rg.bucket_field AS repeat_bucket_field,
            rg.hash_key AS repeat_hash_key,
            rg.count AS repeat_count,
            rg.representative_rowid AS repeat_representative_rowid,
            rg.common_norm_prefix_len AS repeat_common_norm_prefix_len
        FROM merged_windows w
        JOIN native_element_meta m ON m.path = w.path
        JOIN native_element_fts e ON e.rowid = m.rowid
        LEFT JOIN native_element_repeat_best rb ON rb.rowid = e.rowid
        LEFT JOIN native_repeat_group rg ON rg.group_id = rb.group_id
        WHERE CAST(e.element_index AS INTEGER)
            BETWEEN w.start_index AND w.end_index
        ORDER BY w.rank, w.path, w.start_index, CAST(e.element_index AS INTEGER)
    """
    result = native_transcript_index.run_readonly_sql(sql, tuple(params))
    if "interrupted" in str(result.get("error") or ""):
        # Cold page cache can trip the SQL progress-handler deadline on the
        # first FTS query; the retry runs warm, so give it a longer budget.
        result = native_transcript_index.run_readonly_sql(
            sql,
            tuple(params),
            timeout_s=NATIVE_BUNDLE_COLD_RETRY_TIMEOUT_SECONDS,
        )
    if result.get("error"):
        raise RuntimeError(result["error"])
    columns = result.get("columns") or []
    return [dict(zip(columns, row)) for row in result.get("rows") or []]


def _native_bundle_records_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        path = str(row.get("path") or "")
        if not path:
            continue
        try:
            hit_index = int(row.get("hit_index"))
        except (TypeError, ValueError):
            continue
        grouped.setdefault((path, hit_index), []).append(row)

    records: list[dict[str, Any]] = []
    seen_bundle_text: set[tuple[str, str]] = set()
    collapse_state = _native_bundle_collapse_state()
    for (path, hit_index), bundle_rows in grouped.items():
        ordered = sorted(bundle_rows, key=lambda r: int(r.get("element_index") or 0))
        text = _format_native_bundle_text(hit_index, ordered, collapse_state)
        dedupe_key = (path, text)
        if not text or dedupe_key in seen_bundle_text:
            continue
        seen_bundle_text.add(dedupe_key)
        first = ordered[0]
        records.append({
            "source_key": f"native-transcript:{path}:{hit_index}",
            "source_prompt_key": None,
            "unit_index": None,
            "text": text,
            "kind": NATIVE_TRANSCRIPT_BUNDLE_KIND,
            "polarity": "",
            "strength": "medium",
            "source": "native_transcript",
            "source_text": text,
            "prev_reply": "",
            "cwd": first.get("cwd") or "",
            "edited_files": [],
            "git_commits": [],
            "sid": first.get("sid") or "",
            "path": path,
            "ts": first.get("ts_utc") or "",
            "user_seq": None,
            "native_hit_index": hit_index,
        })
    return records


def _native_bundle_collapse_state() -> dict[str, dict[str, Any]]:
    return {"exact": {}, "prefix": {}}


def _native_bundle_row_ref(row: dict[str, Any]) -> str:
    path = str(row.get("path") or "")
    element_index = row.get("element_index")
    return f"{path}:{element_index}"


def _normalize_native_bundle_text(text: str) -> str:
    return " ".join(text.split())


def _native_hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _native_bundle_hash(row: dict[str, Any], field: str, text: str) -> str:
    value = str(row.get(field) or "")
    if value:
        return value
    if field == "text_sha256":
        return _native_hash_text(text)
    normalized = _normalize_native_bundle_text(text)
    if field == "norm_text_sha256":
        return _native_hash_text(normalized) if normalized else ""
    if field.startswith("prefix_") and field.endswith("_sha256"):
        try:
            prefix_len = int(field.removeprefix("prefix_").removesuffix("_sha256"))
        except ValueError:
            return ""
        return _native_hash_text(normalized[:prefix_len]) if normalized else ""
    return ""


def _raw_index_after_normalized_prefix(text: str, prefix_len: int) -> int:
    normalized_len = 0
    emitted_any = False
    in_whitespace = False
    for index, char in enumerate(text):
        if char.isspace():
            if emitted_any and not in_whitespace:
                if normalized_len >= prefix_len:
                    return index
                normalized_len += 1
                in_whitespace = True
            continue
        emitted_any = True
        in_whitespace = False
        if normalized_len >= prefix_len:
            return index
        normalized_len += 1
        if normalized_len >= prefix_len:
            return index + 1
    return len(text)


def _common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    return limit


def _collapse_native_bundle_row_text(
    row: dict[str, Any],
    text: str,
    collapse_state: dict[str, dict[str, Any]],
) -> str:
    row_ref = _native_bundle_row_ref(row)
    repeat_kind = str(row.get("repeat_kind") or "")
    repeat_group_id = row.get("repeat_group_id")
    if repeat_kind and repeat_group_id is not None:
        try:
            group_id = int(repeat_group_id)
            repeat_count = int(row.get("repeat_count") or 0)
            representative_rowid = int(row.get("repeat_representative_rowid") or 0)
        except (TypeError, ValueError):
            group_id = 0
            repeat_count = 0
            representative_rowid = 0
        hash_key = str(row.get("repeat_hash_key") or "")
        if repeat_kind == "exact_text" and group_id:
            return (
                f"<repeated_text_ref group_id={group_id} hash={hash_key[:16]} "
                f"count={repeat_count} representative_rowid={representative_rowid} "
                f"current={row_ref} text_len={len(text)}>"
            )
        if repeat_kind == "shared_prefix" and group_id:
            try:
                prefix_len = int(row.get("repeat_common_norm_prefix_len") or 0)
            except (TypeError, ValueError):
                prefix_len = 0
            raw_tail_start = _raw_index_after_normalized_prefix(text, prefix_len) if prefix_len > 0 else 0
            if 0 < raw_tail_start <= len(text) and prefix_len > 0:
                tail = text[raw_tail_start:]
                ref = (
                    f"<repeated_prefix_ref group_id={group_id} hash={hash_key[:16]} "
                    f"count={repeat_count} representative_rowid={representative_rowid} "
                    f"current={row_ref} prefix_chars={prefix_len} text_len={len(text)}>"
                )
                return f"{ref}\nunique_tail_after_prefix:\n{tail}" if tail else ref

    normalized = _normalize_native_bundle_text(text)
    norm_hash = _native_bundle_hash(row, "norm_text_sha256", text)
    if norm_hash and len(normalized) >= NATIVE_BUNDLE_EXACT_COLLAPSE_MIN_CHARS:
        first_ref = collapse_state["exact"].get(norm_hash)
        if first_ref:
            return (
                f"<repeated_text_ref hash={norm_hash[:16]} "
                f"first={first_ref} current={row_ref} text_len={len(text)}>"
            )
        collapse_state["exact"][norm_hash] = row_ref

    for field, prefix_len in NATIVE_BUNDLE_PREFIX_COLLAPSE_FIELDS:
        prefix_hash = _native_bundle_hash(row, field, text)
        if not prefix_hash or len(normalized) <= prefix_len:
            continue
        first_ref = collapse_state["prefix"].get(prefix_hash)
        if not first_ref:
            collapse_state["prefix"][prefix_hash] = {
                "ref": row_ref,
                "normalized": normalized,
            }
            continue
        common_prefix_len = max(
            prefix_len,
            _common_prefix_len(str(first_ref["normalized"]), normalized),
        )
        if common_prefix_len > prefix_len and normalized[common_prefix_len - 1].isspace():
            common_prefix_len -= 1
        tail = text[_raw_index_after_normalized_prefix(text, common_prefix_len):]
        ref = (
            f"<repeated_prefix_ref field={field} hash={prefix_hash[:16]} "
            f"first={first_ref['ref']} current={row_ref} "
            f"prefix_chars={common_prefix_len} bucket_chars={prefix_len} text_len={len(text)}>"
        )
        return f"{ref}\nunique_tail_after_prefix:\n{tail}" if tail else ref
    return text


def _format_native_bundle_text(
    hit_index: int,
    rows: list[dict[str, Any]],
    collapse_state: dict[str, dict[str, Any]] | None = None,
) -> str:
    if collapse_state is None:
        collapse_state = _native_bundle_collapse_state()
    lines = [
        "Native transcript evidence bundle.",
        "Use this only if the user confirms, adopts, or refines an assistant proposal in the surrounding turns.",
        f"matched_element_index={hit_index}",
    ]
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        element_index = row.get("element_index")
        role = row.get("role") or ""
        kind = row.get("element_kind") or ""
        ts = row.get("ts_utc") or ""
        collapsed_text = _collapse_native_bundle_row_text(row, text, collapse_state)
        lines.append(f"[{element_index} {role} {kind} {ts}] {collapsed_text}")
    return "\n".join(lines)


def _filter_records_by_cwds(records: list[dict[str, Any]], cwds: tuple[str, ...]) -> list[dict[str, Any]]:
    if not cwds:
        return records
    allowed = set(cwds)
    return [record for record in records if record.get("cwd") in allowed]


def _normalize_cwd_filters(
    cwd: str,
    cwds: list[str] | None,
    *,
    all_projects: bool,
) -> tuple[tuple[str, ...], str | None]:
    if all_projects:
        return (), None
    if not isinstance(cwd, str):
        return (), "cwd must be a string"
    if cwds is not None and (
        not isinstance(cwds, list) or any(not isinstance(item, str) for item in cwds)
    ):
        return (), "cwds must be a list of strings"
    normalized: list[str] = []
    for item in [cwd, *(cwds or [])]:
        value = item.strip()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized), None


def _normalize_match_fields(
    fields: list[str] | None,
    *,
    include_all_fields: bool,
) -> tuple[tuple[str, ...] | None, str | None]:
    if include_all_fields:
        return None, None
    if fields is None:
        return DEFAULT_MATCH_FIELDS, None
    normalized: list[str] = []
    available = set(MATCH_FIELD_ORDER)
    for field in fields:
        if not isinstance(field, str):
            return DEFAULT_MATCH_FIELDS, "fields must be a list of strings"
        name = field.strip()
        if not name:
            continue
        if name not in available:
            return DEFAULT_MATCH_FIELDS, f"unsupported field: {name}"
        if name not in normalized:
            normalized.append(name)
    return tuple(normalized or DEFAULT_MATCH_FIELDS), None


def _project_records(records: list[dict[str, Any]], fields: tuple[str, ...] | None) -> list[dict[str, Any]]:
    return [_project_record(record, fields) for record in records]


def _project_record(record: dict[str, Any], fields: tuple[str, ...] | None) -> dict[str, Any]:
    projected = dict(record) if fields is None else {field: record[field] for field in fields if field in record}
    if "edited_files" in projected:
        projected["edited_files"] = _relative_files(projected["edited_files"], record.get("cwd"))
    return projected


def _relative_files(value: Any, cwd: Any) -> Any:
    if not isinstance(value, list) or not isinstance(cwd, str) or not cwd.strip():
        return value
    base = cwd.strip()
    return [_relative_file(path, base) for path in value]


def _relative_file(path: Any, cwd: str) -> Any:
    if not isinstance(path, str) or not path.strip():
        return path
    value = path.strip()
    try:
        common = os.path.commonpath([cwd, value])
    except ValueError:
        return value
    if common != cwd:
        return value
    return os.path.relpath(value, cwd)


def _records_stdout(records: list[dict[str, Any]]) -> str:
    if not records:
        return ""
    return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)


def _write_unit_projection(records: list[dict[str, Any]]) -> tuple[Path, dict[int, dict[str, Any]]]:
    from requirement_analysis.prephase import units_path

    directory = units_path().parent
    fd, raw_path = tempfile.mkstemp(
        prefix=".requirement_units_text.",
        suffix=".txt",
        dir=str(directory),
        text=True,
    )
    path = Path(raw_path)
    line_records: dict[int, dict[str, Any]] = {}
    with open(fd, "w", encoding="utf-8", errors="replace") as handle:
        for idx, record in enumerate(records, start=1):
            handle.write(_unit_search_line(record) + "\n")
            line_records[idx] = record
    return path, line_records


def _write_prompt_fallback_projection(records: list[dict[str, Any]]) -> tuple[Path, dict[int, dict[str, Any]]]:
    from requirement_analysis.prephase import units_path

    directory = units_path().parent
    fd, raw_path = tempfile.mkstemp(
        prefix=".requirement_unprocessed_prompts.",
        suffix=".txt",
        dir=str(directory),
        text=True,
    )
    path = Path(raw_path)
    line_records: dict[int, dict[str, Any]] = {}
    with open(fd, "w", encoding="utf-8", errors="replace") as handle:
        for idx, record in enumerate(records, start=1):
            handle.write(_prompt_fallback_search_line(record) + "\n")
            line_records[idx] = record
    return path, line_records


def _unit_search_line(record: dict[str, Any]) -> str:
    searchable = {
        "text": record.get("text") or "",
        "kind": record.get("kind") or "",
        "origin": record.get("origin") or "",
        "polarity": record.get("polarity") or "",
        "strength": record.get("strength") or "",
        "source": record.get("source") or "",
        "cwd": record.get("cwd") or "",
    }
    return json.dumps(searchable, ensure_ascii=False, sort_keys=True).replace("\r", "\\r").replace("\n", "\\n")


def _prompt_fallback_search_line(record: dict[str, Any]) -> str:
    searchable = {
        "text": record.get("text") or "",
        "prev_reply": record.get("prev_reply") or "",
        "source": record.get("source") or "",
        "cwd": record.get("cwd") or "",
        "edited_files": record.get("edited_files") if isinstance(record.get("edited_files"), list) else [],
    }
    return json.dumps(searchable, ensure_ascii=False, sort_keys=True).replace("\r", "\\r").replace("\n", "\\n")


def _records_from_rg_stdout(
    stdout: str,
    line_records: dict[int, dict[str, Any]],
    limit: int | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in stdout.splitlines():
        line_number = _line_number_from_rg_line(line)
        if line_number is not None:
            record = line_records.get(line_number)
            if record is not None and line_number not in seen:
                seen.add(line_number)
                matches.append(record)
            if _limit_reached(matches, limit):
                break
            continue
        for idx, record in line_records.items():
            if line == _unit_search_line(record) and idx not in seen:
                seen.add(idx)
                matches.append(record)
                break
        if _limit_reached(matches, limit):
            break
    return matches


def _records_from_prompt_rg_stdout(
    stdout: str,
    line_records: dict[int, dict[str, Any]],
    limit: int | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in stdout.splitlines():
        line_number = _line_number_from_rg_line(line)
        if line_number is not None:
            record = line_records.get(line_number)
            if record is not None and line_number not in seen:
                seen.add(line_number)
                matches.append(record)
            if _limit_reached(matches, limit):
                break
            continue
        for idx, record in line_records.items():
            if line == _prompt_fallback_search_line(record) and idx not in seen:
                seen.add(idx)
                matches.append(record)
                break
        if _limit_reached(matches, limit):
            break
    return matches


def _normalize_max_matches(value: int | None) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, int) or isinstance(value, bool):
        return None, "max_matches must be a positive integer when provided"
    if value <= 0:
        return None, "max_matches must be a positive integer when provided"
    return value, None


def _remaining_matches(limit: int | None, current_count: int) -> int | None:
    if limit is None:
        return None
    return max(0, limit - current_count)


def _limit_reached(matches: list[dict[str, Any]], limit: int | None) -> bool:
    return limit is not None and len(matches) >= limit


def _line_number_from_rg_line(line: str) -> int | None:
    raw = line.split(":", 1)[0]
    if not raw.isdigit():
        return None
    return int(raw)


def _normalize_rg_args(rg_args: list[str]) -> list[str]:
    out: list[str] = []
    for arg in rg_args:
        if not isinstance(arg, str):
            continue
        normalized = arg.strip()
        if not normalized:
            continue
        out.append(normalized)
    return out


def _search_rg_args(*, rg_args: list[str] | None, query: str = "") -> tuple[list[str], str]:
    normalized_query = (query or "").strip()
    if rg_args is not None and normalized_query:
        return [], "provide either rg_args or query, not both"
    if normalized_query:
        if len(normalized_query) > RG_QUERY_MAX_CHARS:
            return [], f"query must be at most {RG_QUERY_MAX_CHARS} characters"
        pattern_count = _rg_query_pattern_count(normalized_query)
        if pattern_count == 0:
            return [], "query must include searchable text"
        if pattern_count > RG_QUERY_MAX_PATTERNS:
            return [], f"query must produce at most {RG_QUERY_MAX_PATTERNS} rg patterns"
        return _rg_args_from_query(normalized_query), ""
    if rg_args is None:
        return [], ""
    return _normalize_rg_args(rg_args), ""


def _rg_query_pattern_count(query: str) -> int:
    seen: set[str] = set()
    count = 0
    normalized_query = " ".join(query.split())
    if re.search(r"\w", normalized_query, re.UNICODE):
        seen.add(normalized_query.lower())
        count += 1
    for token in [tok.lower() for tok in UNIT_FTS_TOKEN_RE.findall(normalized_query)]:
        if token in seen:
            continue
        seen.add(token)
        count += 1
    return count


def _rg_args_from_query(query: str) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()
    normalized_query = " ".join(query.split())
    tokens = [tok.lower() for tok in UNIT_FTS_TOKEN_RE.findall(normalized_query)]
    if re.search(r"\w", normalized_query, re.UNICODE):
        seen.add(normalized_query.lower())
        patterns.append(normalized_query)
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        patterns.append(token)
    if not patterns:
        return []
    args = ["-i", "-F"]
    for pattern in patterns:
        args.extend(["-e", pattern])
    return args


def _validate_rg_args(rg_args: list[str]) -> str:
    if "--" in rg_args:
        return "rg_args must not contain --; the backend appends the fixed corpus path"
    bare_patterns = 0
    regexp_patterns = 0
    i = 0
    while i < len(rg_args):
        arg = rg_args[i]
        if arg in RG_FORBIDDEN_OPTIONS or any(
            arg.startswith(option + "=") for option in RG_FORBIDDEN_OPTIONS if option.startswith("--")
        ) or (arg.startswith("-f") and arg != "-F"):
            forbidden = "-f" if arg.startswith("-f") and arg != "-F" else arg.split("=", 1)[0]
            return f"{forbidden} is not allowed for requirement-unit rg"
        if arg in RG_OPTIONS_WITH_VALUE:
            if i + 1 >= len(rg_args):
                return f"{arg} requires a value"
            if arg in ("-e", "--regexp"):
                regexp_patterns += 1
            i += 2
            continue
        if any(arg.startswith(prefix + "=") for prefix in RG_OPTIONS_WITH_VALUE if prefix.startswith("--")):
            if arg.startswith("--regexp="):
                regexp_patterns += 1
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        bare_patterns += 1
        if bare_patterns > 1:
            return "rg_args must include patterns but no paths; the backend appends the corpus path"
        i += 1
    if regexp_patterns and bare_patterns:
        return "rg_args with -e/--regexp must not include positional paths; the backend appends the corpus path"
    if regexp_patterns == 0 and bare_patterns == 0:
        return "rg_args must include a pattern, for example ['-i', 'exact user text']"
    return ""


def _safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import provisioning
import extension_package_loader
import extension_store
from provisioning import DirtyPolicy, ProvisionedSessionSpec
from provisioning.prompts import render_prompt

RG_TIMEOUT_SECONDS = 30
DEFAULT_MATCH_FIELDS = ("text", "kind", "polarity", "strength", "source", "cwd", "ts")
MATCH_FIELD_ORDER = (
    "source_key",
    "source_prompt_key",
    "unit_index",
    "text",
    "kind",
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
PROCESSOR_PARSE_ATTEMPTS = 3
PROCESSOR_REQUIREMENT_FIELDS = ("text", "kind", "polarity", "strength", "source", "cwd")
NATIVE_BUNDLE_HIT_LIMIT = 6
NATIVE_BUNDLE_WINDOW_BEFORE = 5
NATIVE_BUNDLE_WINDOW_AFTER = 8
RG_OPTIONS_WITH_VALUE = {
    "-A",
    "-B",
    "-C",
    "-E",
    "-e",
    "-f",
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


class GetRequirementsProcessorSpec(ProvisionedSessionSpec):
    key = GET_REQUIREMENTS_PROCESSOR_KEY
    version = 5
    name = "worker:requirements:query-processor"
    env_prefix = "GET_REQUIREMENTS_PROCESSOR"
    task_key = "requirement_analysis"
    orchestration_mode = "native"
    bare_config = False
    worker_creation_policy = "deny"
    machine_completion = False
    run_mode = "fork"
    ephemeral_forks = True
    dispatch = "http"
    on_no_fork = "error"
    provision_timeout = 90.0
    retry_attempts = 1
    dirty_policy = DirtyPolicy(
        max_base_bytes=5_000_000,
        max_user_turns=None,
        max_assistant_turns=None,
    )

    def build_provision_prompt(self, ctx: dict) -> str:
        return render_prompt("get_requirements_processor.md", {})

    def build_instructions(self, query: str, ctx: dict) -> str:
        request = {
            "query": query,
            "cwd": ctx.get("cwd") or "",
            "cwds": ctx.get("cwds") or [],
            "all_projects": bool(ctx.get("all_projects")),
            "max_matches": ctx.get("max_matches"),
        }
        return (
            "Find the related stored requirements for this request.\n"
            "Call the get_requirements_internal MCP tool directly. Do not call the get-requirements skill "
            "or public get_requirements tool from inside this processor.\n"
            "Build rg_args for ripgrep over a backend-owned corpus: pass search options and patterns only, "
            "never file paths. For multiple patterns use -e/--regexp for every pattern, for example "
            "['-i', '-e', 'session search', '-e', 'parse_failed']; do not pass bare token lists like "
            "['session', 'search', 'parse_failed'].\n"
            "Use broad key phrases from request.query. Extra query words may be noisy, so do not require "
            "every term to match. Treat raw matches as candidate requirements and return any match that is "
            "semantically related to the request or to a concrete failure/tool/provider named in it. "
            "Matches with kind=native_transcript_bundle are raw transcript evidence: read the assistant "
            "proposal and following user turns in the bundle, then return the requirement only when the "
            "user confirms, adopts, or refines it; return the refined user-approved requirement text. "
            "Use cwd/cwds/all_projects/max_matches from the request.\n"
            "Each match carries its full timestamp in the `ts` field, and matches are ordered oldest-first "
            "by `ts`. Read them in that chronological order: it shows how the requirement evolved over time, "
            "so a later prompt refines or overrides an earlier one on the same topic — weight the latest "
            "statement accordingly.\n"
            "Return only the required JSON object.\n"
            f"<request>\n{json.dumps(request, ensure_ascii=False)}\n</request>"
        )

    def parse_result(self, text: str, ctx: dict) -> dict[str, Any]:
        obj = _parse_valid_processor_json(text)
        if obj is None:
            return _processor_parse_failed()
        requirements = obj["requirements"]
        return {"requirements": _normalize_processed_requirements(requirements)}


GET_REQUIREMENTS_PROCESSOR_SPEC = provisioning.register(GetRequirementsProcessorSpec())


def _requirements_package_root() -> Path:
    return extension_package_loader.package_root(extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID)


def _ensure_requirements_importable() -> Path:
    return extension_package_loader.ensure_package_importable(
        extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
        "requirement_analysis",
    )


def get_processed_requirements(
    *,
    query: str,
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    max_matches: int | None = 20,
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
        max_matches=max_matches,
    )
    requirements = processed.get("requirements") if isinstance(processed, dict) else []
    if not isinstance(requirements, list):
        requirements = []
    error = processed.get("error") if isinstance(processed, dict) else "processor_failed"
    response = {
        "success": not bool(error),
        "requirements": requirements,
        "count": len(requirements),
    }
    if error:
        response["error"] = error
    return response


def _run_requirements_processor(
    *,
    query: str,
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    max_matches: int | None = 20,
) -> dict[str, Any]:
    ctx = {
        "cwd": cwd,
        "cwds": cwds or [],
        "all_projects": all_projects,
        "max_matches": max_matches,
    }
    for _attempt in range(PROCESSOR_PARSE_ATTEMPTS):
        try:
            result = provisioning.run_sync(GET_REQUIREMENTS_PROCESSOR_SPEC, query, ctx)
        except Exception as exc:
            return {"requirements": [], "error": _processor_failure_message(exc)}
        value = result.value if isinstance(result.value, dict) else _processor_parse_failed()
        if value.get("error") != "parse_failed":
            return value
    return _processor_parse_failed()


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
    if _is_explicit_rate_limit_error(lower):
        return (
            "processor_failed: get-requirements processor hit a provider rate limit; "
            "no retry attempted"
        )
    if isinstance(exc, TimeoutError) or "timed out" in lower or "timeout" in lower:
        return (
            "processor_failed: get-requirements processor timed out before returning requirements; "
            "no retry attempted"
        )
    suffix = f": {error_text}" if error_text else ""
    return f"processor_failed: {type(exc).__name__}{suffix}"


def _is_explicit_rate_limit_error(lower_error_text: str) -> bool:
    return any(marker in lower_error_text for marker in _RATE_LIMIT_MARKERS)


def _processor_parse_failed() -> dict[str, Any]:
    return {"requirements": [], "error": "parse_failed"}


def _parse_valid_processor_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if _is_valid_processor_payload(parsed) else None
    except json.JSONDecodeError:
        pass
    for candidate in _json_object_candidates_from_end(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if _is_valid_processor_payload(parsed):
            return parsed
    return None


def _json_object_candidates_from_end(text: str) -> list[str]:
    candidates: list[str] = []
    stack: list[int] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append(index)
        elif char == "}" and stack:
            start = stack.pop()
            if not stack:
                candidates.append(text[start:index + 1])
    return list(reversed(candidates))


def _is_valid_processor_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    requirements = value.get("requirements")
    if not isinstance(requirements, list):
        return False
    return all(_is_valid_processor_requirement(item) for item in requirements)


def _is_valid_processor_requirement(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required_fields_valid = all(
        _is_nonempty_string(value.get(field))
        for field in PROCESSOR_REQUIREMENT_FIELDS
        if field != "polarity"
    )
    polarity = value.get("polarity")
    return required_fields_valid and (polarity is None or isinstance(polarity, str))


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_processed_requirements(matches: list[Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    requirements: list[dict[str, Any]] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        text = str(match.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        requirement = {
            "text": text,
        }
        for key in ("kind", "polarity", "strength", "source", "cwd", "ts"):
            value = match.get(key)
            if key == "polarity" and value is None:
                value = ""
            if value is not None:
                requirement[key] = value
        requirements.append(requirement)
    return requirements


def _sort_matches_by_ts_asc(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order matches oldest-first by timestamp so the requirement-building LLM
    reads a requirement's evolution over time; matches without a timestamp sort
    last."""
    return sorted(matches, key=lambda m: (not m.get("ts"), m.get("ts") or ""))


def search_requirements(
    *,
    rg_args: list[str],
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    fields: list[str] | None = None,
    include_all_fields: bool = False,
    include_unprocessed_prompts: bool = False,
    max_matches: int | None = None,
) -> dict[str, Any]:
    _ensure_requirements_importable()
    normalized_args = _normalize_rg_args(rg_args)
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

    preparation = prepare_requirements_context()
    return _search_requirements_prepared(
        rg_args=normalized_args,
        cwd=cwd,
        cwds=cwds,
        all_projects=all_projects,
        fields=fields,
        include_all_fields=include_all_fields,
        include_unprocessed_prompts=include_unprocessed_prompts,
        max_matches=max_matches,
        preparation=preparation,
    )


def _search_requirements_prepared(
    *,
    rg_args: list[str],
    cwd: str = "",
    cwds: list[str] | None = None,
    all_projects: bool = False,
    fields: list[str] | None = None,
    include_all_fields: bool = False,
    include_unprocessed_prompts: bool = False,
    max_matches: int | None = None,
    preparation: dict[str, Any],
) -> dict[str, Any]:
    normalized_args = _normalize_rg_args(rg_args)
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
            "freshness": freshness,
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
        "freshness": freshness,
    }


def prepare_requirements_context(
    *,
    allowed_unhandled_prompts: int = 1,
) -> dict[str, Any]:
    """Cheap, non-blocking refresh on the get-requirements query path.

    Syncs the raw user_prompts corpus (cheap) and reports current unit
    freshness, then ensures the detached extraction runner is alive — but
    NEVER runs unit extraction or the downstream DAG inline. The background
    runner owns extraction+downstream to completion; the query path answers
    best-effort from whatever units already exist plus a raw-prompt search."""
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
        from requirement_analysis import cli

        backend_dir = Path(__file__).resolve().parent
        return cli.launch_background(
            ["--extract", "--background"],
            extra_pythonpath=[str(_requirements_package_root()), str(backend_dir)],
        )
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
    from requirement_analysis.prompts import load_prompts, prompt_key

    keys = freshness.get("unhandled_prompt_keys")
    if not isinstance(keys, list):
        return []
    unhandled_keys = {key for key in keys if isinstance(key, str)}
    if not unhandled_keys:
        return []
    records: list[dict[str, Any]] = []
    for prompt in load_prompts():
        key = prompt.get("key") or prompt_key(prompt)
        if key not in unhandled_keys:
            continue
        records.append(_prompt_fallback_record(prompt, key))
    return records


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
        quick_state = native_transcript_index.quick_state()
        if not quick_state.get("covered"):
            return _native_bundle_result([], searched=False, reason="index_not_usable", index=quick_state)
        native_transcript_index.ensure_started()
        if native_transcript_index.is_covered() and not native_transcript_index.is_usable():
            native_transcript_index.request_refresh()
            native_transcript_index.wait_fresh()
        index_state = {
            "covered": native_transcript_index.is_covered(),
            "usable": native_transcript_index.is_usable(),
        }
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
    params.extend([limit, NATIVE_BUNDLE_WINDOW_BEFORE, NATIVE_BUNDLE_WINDOW_AFTER])
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
        )
        SELECT
            h.hit_index,
            e.text,
            e.path,
            e.sid,
            e.cwd,
            e.tag,
            e.element_kind,
            e.tool_name,
            e.ts,
            e.role,
            e.element_id,
            e.element_index
        FROM hits h
        JOIN native_element_fts e ON e.path = h.path
        WHERE CAST(e.element_index AS INTEGER)
            BETWEEN h.hit_index - ? AND h.hit_index + ?
        ORDER BY h.rank, h.path, h.hit_index, CAST(e.element_index AS INTEGER)
    """
    result = native_transcript_index.run_readonly_sql(
        sql,
        tuple(params),
        row_limit=max(1, limit) * (NATIVE_BUNDLE_WINDOW_BEFORE + NATIVE_BUNDLE_WINDOW_AFTER + 1),
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
    seen_text: set[str] = set()
    for (path, hit_index), bundle_rows in grouped.items():
        ordered = sorted(bundle_rows, key=lambda r: int(r.get("element_index") or 0))
        text = _format_native_bundle_text(hit_index, ordered)
        if not text or text in seen_text:
            continue
        seen_text.add(text)
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
            "ts": first.get("ts") or "",
            "user_seq": None,
            "native_hit_index": hit_index,
        })
    return records


def _format_native_bundle_text(hit_index: int, rows: list[dict[str, Any]]) -> str:
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
        ts = row.get("ts") or ""
        lines.append(f"[{element_index} {role} {kind} {ts}] {text}")
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


def _validate_rg_args(rg_args: list[str]) -> str:
    if "--" in rg_args:
        return "rg_args must not contain --; the backend appends the fixed corpus path"
    bare_patterns = 0
    regexp_patterns = 0
    i = 0
    while i < len(rg_args):
        arg = rg_args[i]
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

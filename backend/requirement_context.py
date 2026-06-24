from __future__ import annotations

import importlib.util
import json
import os
import re
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
DEFAULT_MATCH_FIELDS = ("text", "kind", "polarity", "strength", "source", "cwd")
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
    "ts",
    "user_seq",
)
PROMPT_FALLBACK_KIND = "unprocessed_prompt"
GET_REQUIREMENTS_PROCESSOR_KEY = "get_requirements_processor"
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
    version = 1
    name = "worker:requirements:query-processor"
    env_prefix = "GET_REQUIREMENTS_PROCESSOR"
    task_key = "requirement_analysis"
    orchestration_mode = "native"
    bare_config = False
    worker_creation_policy = "deny"
    machine_completion = False
    run_mode = "direct"
    ephemeral_forks = False
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
            "Call get_requirements_internal with rg_args derived from request.query. "
            "Use cwd/cwds/all_projects/max_matches from the request. "
            "Return only the required JSON object.\n"
            f"<request>\n{json.dumps(request, ensure_ascii=False)}\n</request>"
        )

    def parse_result(self, text: str, ctx: dict) -> dict[str, Any]:
        obj = _parse_processor_json(text)
        if not isinstance(obj, dict):
            return {"requirements": [], "error": "parse_failed"}
        requirements = obj.get("requirements")
        if not isinstance(requirements, list):
            return {"requirements": [], "error": "parse_failed"}
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
    # Strategy A: refresh the incremental cache on-demand inside the call
    # (no background prewarm). Self-guarded — never raises; the processor
    # queries whatever units are on disk.
    prepare_requirements_context()
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
    response = {
        "success": not bool(processed.get("error")) if isinstance(processed, dict) else False,
        "requirements": requirements,
        "count": len(requirements),
    }
    error = processed.get("error") if isinstance(processed, dict) else "processor_failed"
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
    try:
        result = provisioning.run_sync(GET_REQUIREMENTS_PROCESSOR_SPEC, query, ctx)
    except Exception as exc:
        return {"requirements": [], "error": _processor_failure_message(exc)}
    value = result.value
    return value if isinstance(value, dict) else {"requirements": [], "error": "parse_failed"}


def _processor_failure_message(exc: Exception) -> str:
    error_text = str(exc).strip()
    lower = error_text.lower()
    if isinstance(exc, TimeoutError) or "timed out" in lower or "timeout" in lower:
        return (
            "processor_failed: get-requirements processor timed out; "
            "provider may be rate limited or unavailable; no retry attempted"
        )
    if "rate_limit" in lower or "rate limit" in lower or "429" in lower:
        return (
            "processor_failed: get-requirements processor hit a provider rate limit; "
            "no retry attempted"
        )
    suffix = f": {error_text}" if error_text else ""
    return f"processor_failed: {type(exc).__name__}{suffix}"


def _parse_processor_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    matches = list(re.finditer(r"\{[\s\S]*\}", text))
    for match in reversed(matches):
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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
        for key in ("kind", "polarity", "strength", "source", "cwd"):
            value = match.get(key)
            if value is not None:
                requirement[key] = value
        requirements.append(requirement)
    return requirements


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

#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import _test_home

_test_home.isolate("ba-extension-context-audit-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import extension_context_audit as audit  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def projection(
    *,
    provider_kind: str = "codex",
    profile_id: str = "default",
    runtime_names: list[str] | None = None,
    capability_ids: list[tuple[str, str]] | None = None,
) -> dict:
    return audit.build_projection(
        provider_kind=provider_kind,
        profile_id=profile_id,
        profile_revision="7",
        runtime_skills=[{"name": name} for name in runtime_names or ["review"]],
        capability_contexts=[
            {"source_id": source_id, "capability_id": capability_id}
            for source_id, capability_id in capability_ids or []
        ],
        extension_mcp_servers={"ext.tools": ["search"]},
        extension_skills={"ext.skills": ["verify"]},
        extension_instruction_names={"ext.rules": ["security"]},
    )


def reset_state() -> None:
    audit._IN_FLIGHT.clear()  # type: ignore[attr-defined]
    audit._FAILURES.clear()  # type: ignore[attr-defined]
    audit._CACHE_PROJECTION.clear()  # type: ignore[attr-defined]
    shutil.rmtree(audit._cache_dir(), ignore_errors=True)  # type: ignore[attr-defined]


def finding_result(value: dict) -> dict:
    first, second = value["entries"][:2]
    return {
        "status": "findings",
        "findings": [{"code": "potential_overlap", "refs": [first["ref"], second["ref"]]}],
    }


def t_projection_is_exact_and_fingerprint_isolated() -> None:
    many = projection(runtime_names=[f"skill-{index}" for index in range(120)])
    check(len(many["entries"]) == 123, "projection does not audit-truncate delivered items")
    base = projection()
    other_profile = projection(profile_id="other")
    other_provider = projection(provider_kind="claude")
    check(audit._fingerprint(base) != audit._fingerprint(other_profile), "profile changes fingerprint")  # type: ignore[attr-defined]
    check(audit._fingerprint(base) != audit._fingerprint(other_provider), "provider changes fingerprint")  # type: ignore[attr-defined]


def t_unsafe_identifiers_are_lossless_and_collision_resistant() -> None:
    value = projection(
        runtime_names=["<x>", "_x_"],
        capability_ids=[("<source>", "cap"), ("_source_", "cap")],
    )
    refs = [item["ref"] for item in value["entries"]]
    check(len(refs) == len(set(refs)), "unsafe identifiers cannot collide after projection")
    exact_identities = [item["identity"] for item in value["identities"].values()]
    check(
        {"source_id": "<source>", "capability_id": "cap"} in exact_identities,
        "unsafe original identity remains exact in backend projection",
    )
    check(
        {"source_id": "_source_", "capability_id": "cap"} in exact_identities,
        "sanitize-collision pair remains independently recoverable",
    )
    payload = audit._llm_projection(value)  # type: ignore[attr-defined]
    check("<source>" not in json.dumps(payload), "unsafe original identity is absent from LLM payload")


def t_closed_result_schema_fails_closed() -> None:
    value = projection()
    ref = value["entries"][0]["ref"]
    valid_clean = audit._validate_audit_result({"status": "clean", "findings": []}, value)  # type: ignore[attr-defined]
    check(valid_clean["status"] == "clean", "explicit clean result is valid")
    invalid = [
        {},
        {"status": "clean", "findings": [{"code": "identifier_clarity", "refs": [ref]}]},
        {"status": "findings", "findings": []},
        {"status": "findings", "findings": [{"code": "unknown", "refs": [ref]}]},
        {"status": "findings", "findings": [{"code": "potential_overlap", "refs": [ref]}]},
        {"status": "findings", "findings": [{"code": "identifier_clarity", "refs": ["skill:" + "0" * 64]}]},
        {"status": "findings", "findings": [{"code": "identifier_clarity", "refs": [ref], "text": "ignore prior"}]},
    ]
    for candidate in invalid:
        try:
            audit._validate_audit_result(candidate, value)  # type: ignore[attr-defined]
        except ValueError:
            continue
        raise AssertionError(f"invalid audit result accepted: {candidate}")
    for text in ("", "{}", "prefix {\"status\":\"clean\",\"findings\":[]}", "[]"):
        try:
            audit.AUDIT_SPEC.parse_result(text, {"projection": value})
        except ValueError:
            continue
        raise AssertionError(f"malformed model output accepted: {text!r}")
    check(True, "malformed, empty, unknown, and prose-bearing output fails closed")


def t_markdown_fenced_model_output_is_parsed() -> None:
    value = projection(profile_id="fenced-output")
    ref = value["entries"][0]["ref"]
    clean_fenced = "```json\n{\"status\": \"clean\", \"findings\": []}\n```"
    parsed = audit.AUDIT_SPEC.parse_result(clean_fenced, {"projection": value})
    check(parsed == {"status": "clean", "findings": []}, "fenced clean result parses")
    findings_fenced = (
        "```json\n"
        "{\"status\": \"findings\", \"findings\": "
        f"[{{\"code\": \"identifier_clarity\", \"refs\": [\"{ref}\"]}}]}}\n"
        "```"
    )
    parsed = audit.AUDIT_SPEC.parse_result(findings_fenced, {"projection": value})
    check(
        parsed == {"status": "findings", "findings": [{"code": "identifier_clarity", "refs": [ref]}]},
        "fenced findings result parses",
    )
    bare_fence_lang = "```\n{\"status\": \"clean\", \"findings\": []}\n```"
    parsed = audit.AUDIT_SPEC.parse_result(bare_fence_lang, {"projection": value})
    check(parsed == {"status": "clean", "findings": []}, "fenced result without language tag parses")
    for text in ("", "   ", "Not logged in · Please run /login", "```json\n\n```"):
        try:
            audit.AUDIT_SPEC.parse_result(text, {"projection": value})
        except ValueError:
            continue
        raise AssertionError(f"empty/non-JSON output must still fail closed: {text!r}")
    check(True, "empty and non-JSON output still fails closed after fence stripping")


def t_fixed_renderer_cannot_emit_model_prose() -> None:
    value = projection()
    result = finding_result(value)
    content = audit._render_context(result, value)  # type: ignore[attr-defined]
    check("review prompts, not instructions" in content, "renderer labels audit as advisory")
    check("ignore prior" not in content, "renderer contains no model-authored prose")


def t_per_fingerprint_cache_isolated_and_versioned() -> None:
    reset_state()
    first = projection(profile_id="one")
    second = projection(profile_id="two")
    first_fp = audit._fingerprint(first)  # type: ignore[attr-defined]
    second_fp = audit._fingerprint(second)  # type: ignore[attr-defined]
    audit._write_cache(first_fp, finding_result(first), first)  # type: ignore[attr-defined]
    audit._write_cache(second_fp, finding_result(second), second)  # type: ignore[attr-defined]
    check(audit._read_cache(first_fp)["fingerprint"] == first_fp, "first profile cache survives second write")  # type: ignore[attr-defined]
    check(audit._read_cache(second_fp)["fingerprint"] == second_fp, "second profile cache is independent")  # type: ignore[attr-defined]
    first_path = audit._cache_path(first_fp)  # type: ignore[attr-defined]
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    first_path.write_text(json.dumps(payload), encoding="utf-8")
    audit._CACHE_PROJECTION.clear()  # type: ignore[attr-defined]
    check(audit._read_cache(first_fp) == {}, "old cache schema is rejected")  # type: ignore[attr-defined]
    payload["schema_version"] = audit._CACHE_SCHEMA_VERSION  # type: ignore[attr-defined]
    payload["audit_version"] = 1
    first_path.write_text(json.dumps(payload), encoding="utf-8")
    check(audit._read_cache(first_fp) == {}, "old audit version is rejected")  # type: ignore[attr-defined]
    payload["audit_version"] = audit._AUDIT_VERSION  # type: ignore[attr-defined]
    payload["fingerprint"] = second_fp
    first_path.write_text(json.dumps(payload), encoding="utf-8")
    check(audit._read_cache(first_fp) == {}, "filename and payload fingerprint must match")  # type: ignore[attr-defined]
    try:
        audit._write_cache(  # type: ignore[attr-defined]
            second_fp,
            {"status": "findings", "findings": [{"code": "unknown", "refs": []}]},
            second,
        )
    except ValueError:
        check(True, "cache publication rejects unvalidated results")
    else:
        raise AssertionError("cache publication accepted an invalid result")


def t_refresh_backoff_and_success_reset() -> None:
    reset_state()
    value = projection()
    fingerprint = audit._fingerprint(value)  # type: ignore[attr-defined]
    original_run_sync = audit.provisioning.run_sync
    try:
        audit.provisioning.run_sync = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("nope"))
        audit._refresh_cache(fingerprint, value)  # type: ignore[attr-defined]
        check(fingerprint in audit._FAILURES, "failed refresh enters backoff")  # type: ignore[attr-defined]
        original_thread = audit.threading.Thread
        started: list[bool] = []
        audit.threading.Thread = lambda **_kwargs: started.append(True)  # type: ignore[assignment]
        audit._trigger_refresh(fingerprint, value)  # type: ignore[attr-defined]
        check(started == [], "backoff suppresses repeated refresh")
        audit.threading.Thread = original_thread  # type: ignore[assignment]
        audit.provisioning.run_sync = lambda *_args, **_kwargs: SimpleNamespace(value={"status": "clean", "findings": []})
        audit._FAILURES[fingerprint] = (1, 0.0)  # type: ignore[attr-defined]
        audit._refresh_cache(fingerprint, value)  # type: ignore[attr-defined]
        check(fingerprint not in audit._FAILURES, "successful refresh resets backoff")  # type: ignore[attr-defined]
    finally:
        audit.provisioning.run_sync = original_run_sync
        audit.threading.Thread = original_thread  # type: ignore[assignment]


def t_invalid_model_result_enters_backoff_without_cache() -> None:
    reset_state()
    value = projection(profile_id="invalid-model-result")
    fingerprint = audit._fingerprint(value)  # type: ignore[attr-defined]
    original_run_sync = audit.provisioning.run_sync
    try:
        audit.provisioning.run_sync = lambda *_args, **_kwargs: SimpleNamespace(value={})
        audit._refresh_cache(fingerprint, value)  # type: ignore[attr-defined]
    finally:
        audit.provisioning.run_sync = original_run_sync
    check(fingerprint in audit._FAILURES, "invalid model result enters backoff")  # type: ignore[attr-defined]
    check(not audit._cache_path(fingerprint).exists(), "invalid model result is never cached")  # type: ignore[attr-defined]


def t_thread_cache_writes_are_atomic_and_bounded() -> None:
    errors: list[BaseException] = []

    def write(index: int) -> None:
        try:
            value = projection(profile_id="thread-shared" if index % 2 == 0 else f"thread-{index}")
            fingerprint = audit._fingerprint(value)  # type: ignore[attr-defined]
            audit._write_cache(fingerprint, {"status": "clean", "findings": []}, value)  # type: ignore[attr-defined]
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(80)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check(errors == [], "same-process concurrent cache writers complete")
    _assert_cache_is_atomic_and_bounded("thread")
    disk_fingerprints = {path.stem for path in audit._cache_dir().glob("*.json")}  # type: ignore[attr-defined]
    memory_fingerprints = set(audit._CACHE_PROJECTION)  # type: ignore[attr-defined]
    check(len(memory_fingerprints) <= audit._MAX_CACHE_ENTRIES, "thread memory cache is bounded")  # type: ignore[attr-defined]
    check(memory_fingerprints <= disk_fingerprints, "locally evicted files leave no stale memory entry")


def t_cross_process_cache_writes_are_atomic_and_bounded() -> None:
    cache_home = str(audit.bc_home())  # type: ignore[attr-defined]
    env = {
        **os.environ,
        "BETTER_AGENT_HOME": cache_home,
        "PYTHONPATH": str(ROOT),
    }
    code = (
        "import extension_context_audit as a,sys;"
        "p=a.build_projection(provider_kind='codex',profile_id=sys.argv[1],profile_revision='1',"
        "runtime_skills=[{'name':'review'}],capability_contexts=[],extension_mcp_servers={},"
        "extension_skills={},extension_instruction_names={});"
        "fp=a._fingerprint(p);"
        "a._write_cache(fp,{'status':'clean','findings':[]},p)"
    )
    processes = []
    for index in range(72):
        processes.append(subprocess.Popen([sys.executable, "-c", code, f"process-{index}"], env=env))
    return_codes = [process.wait(timeout=30) for process in processes]
    check(all(code == 0 for code in return_codes), "all cross-process cache writers completed")
    _assert_cache_is_atomic_and_bounded("cross-process")


def _assert_cache_is_atomic_and_bounded(label: str) -> None:
    cache_files = list(audit._cache_dir().glob("*.json"))  # type: ignore[attr-defined]
    check(len(cache_files) <= audit._MAX_CACHE_ENTRIES, f"{label} eviction enforces cache bound")  # type: ignore[attr-defined]
    for path in cache_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["fingerprint"] != path.stem:
            raise AssertionError(f"{label} cache file does not match filename: {path}")
    check(True, f"{label} cache files are complete and matched")


def t_hot_path_never_runs_model_or_sleeps() -> None:
    reset_state()
    value = projection(profile_id="hot-path")
    original_trigger = audit._trigger_refresh  # type: ignore[attr-defined]
    original_sleep = audit.time.sleep
    calls: list[str] = []
    try:
        audit._trigger_refresh = lambda fingerprint, _projection: calls.append(fingerprint)  # type: ignore[attr-defined]
        audit.time.sleep = lambda *_args: (_ for _ in ()).throw(AssertionError("hot path slept"))
        started = time.monotonic()
        contexts = audit.runtime_context(value)
        elapsed = time.monotonic() - started
    finally:
        audit._trigger_refresh = original_trigger  # type: ignore[attr-defined]
        audit.time.sleep = original_sleep
    check(contexts == [] and len(calls) == 1, "cold hot path only schedules refresh")
    check(elapsed < 0.1, "cold hot path stays latency-bounded")


def t_provider_readiness_is_not_on_turn_hot_path() -> None:
    reset_state()
    value = projection(profile_id="readiness-off-hot-path")
    original_trigger = audit._trigger_refresh  # type: ignore[attr-defined]
    calls: list[str] = []
    try:
        audit._trigger_refresh = lambda fingerprint, _projection: calls.append(fingerprint)  # type: ignore[attr-defined]
        contexts = audit.runtime_context(value)
    finally:
        audit._trigger_refresh = original_trigger  # type: ignore[attr-defined]
    check(contexts == [] and len(calls) == 1, "provider readiness is delegated to refresh")


def t_turn_path_reads_cache_without_starting_provisioning() -> None:
    reset_state()
    value = projection(profile_id="turn-cache-only")
    original_trigger = audit._trigger_refresh  # type: ignore[attr-defined]
    calls: list[str] = []
    try:
        audit._trigger_refresh = lambda fingerprint, _projection: calls.append(fingerprint)  # type: ignore[attr-defined]
        contexts = audit.runtime_context(value, request_refresh=False)
    finally:
        audit._trigger_refresh = original_trigger  # type: ignore[attr-defined]
    check(contexts == [] and calls == [], "turn cache miss does not start audit provisioning")


def t_turn_manager_uses_paired_projection_on_both_paths() -> None:
    source = (ROOT / "turn_manager.py").read_text(encoding="utf-8")
    check(source.count("runtime_skill_projection,") == 2, "initial and refresh paths share runtime projection")
    check(source.count("provider_capability_projection(") >= 2, "initial and refresh paths share provider projection")
    check("runtime_skill_contexts," not in source, "legacy lossy runtime path is absent")
    check(source.count("request_refresh=False") == 2, "both turn paths are audit-cache-only")


def main() -> None:
    try:
        t_projection_is_exact_and_fingerprint_isolated()
        t_unsafe_identifiers_are_lossless_and_collision_resistant()
        t_closed_result_schema_fails_closed()
        t_markdown_fenced_model_output_is_parsed()
        t_fixed_renderer_cannot_emit_model_prose()
        t_per_fingerprint_cache_isolated_and_versioned()
        t_refresh_backoff_and_success_reset()
        t_invalid_model_result_enters_backoff_without_cache()
        t_thread_cache_writes_are_atomic_and_bounded()
        t_cross_process_cache_writes_are_atomic_and_bounded()
        t_hot_path_never_runs_model_or_sleeps()
        t_provider_readiness_is_not_on_turn_hot_path()
        t_turn_path_reads_cache_without_starting_provisioning()
        t_turn_manager_uses_paired_projection_on_both_paths()
    finally:
        shutil.rmtree(audit.bc_home(), ignore_errors=True)  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()

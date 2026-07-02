#!/usr/bin/env python3
"""Locks the requirements execution-model redesign:

1. get-requirements query path is cheap + non-blocking: public and processor
   raw lookups use local-read preparation while the detached background runner
   owns prompt sync, unit extraction, and downstream DAG work.
2. The detached runner is spawned with an injected PYTHONPATH so its child
   interpreter can import the requirements package + backend modules.
3. The batch packer falls back to a feasible greedy packing when the MILP
   solver fails (e.g. HiGHS time-limit on a large backlog) instead of raising.
4. The processor's internal search tool always includes the not-yet-extracted
   raw prompts (best-effort while the backlog drains).
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]                       # .../backend
REPO = ROOT.parent
PKG_ROOT = REPO / "better-agent-private" / "extensions" / "requirements"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PKG_ROOT))
sys.path.insert(0, str(REPO / "sdk"))

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-req-query-path-"))
import _test_home
_test_home.isolate("ba-test-")

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        FAILURES.append(label)


def test_greedy_packing_respects_capacity_and_cap() -> None:
    from requirement_analysis import prephase

    Item = prephase._PackItem
    # Four 60-token items, two bins of 100: each bin holds one (120 > 100), so
    # 2 fit and 2 overflow → dropped (stay pending, no extra bin opened).
    items = [Item(index=i, plan=None, size=60, cluster_index=0) for i in range(4)]
    bins = prephase._greedy_bin_packing(items, max_bins=2, max_input_tokens=100)
    check(len(bins) <= 2, "greedy never exceeds max_bins")
    check(all(sum(it.size for it in b) <= 100 for b in bins), "no bin exceeds capacity")
    placed = [it.index for b in bins for it in b]
    check(len(placed) == len(set(placed)), "each item placed at most once")
    check(len(placed) < len(items), "overflow items dropped when max_bins reached")


def test_milp_failure_falls_back_to_greedy() -> None:
    from requirement_analysis import prephase
    import scipy.optimize as opt

    Item = prephase._PackItem
    items = [Item(index=i, plan=None, size=40, cluster_index=0) for i in range(4)]
    orig = opt.milp
    opt.milp = lambda *a, **k: types.SimpleNamespace(
        success=False, message="Time limit reached. (HiGHS Status 13)", x=None
    )
    try:
        bins = prephase._solve_token_bin_packing_milp(
            items, cluster_count=1, max_bins=4, max_input_tokens=100
        )
    finally:
        opt.milp = orig
    check(isinstance(bins, list) and bins, "MILP timeout falls back to a non-empty greedy packing (no raise)")
    check(all(sum(it.size for it in b) <= 100 for b in bins), "greedy fallback respects capacity")


def test_query_path_has_no_inline_extraction() -> None:
    import requirement_context as rc

    src = inspect.getsource(rc.prepare_requirements_context)
    check("sync_units" not in src, "prepare_requirements_context never calls sync_units")
    check("_ensure_requirement_units_current" not in src, "inline extraction step removed")
    check("_ensure_background_extraction" in src, "prepare ensures the background runner instead")
    check(not hasattr(rc, "_maybe_run_downstream_in_background"), "in-backend downstream daemon removed")
    check(not hasattr(rc, "_sync_requirement_units"), "inline unit-sync helper removed")


def test_public_get_requirements_keeps_processor_off_sync_path() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_context,
        "local_prepare": rc.prepare_requirements_local_read_context,
        "run_sync": rc.provisioning.run_sync,
        "ensure_importable": rc._ensure_requirements_importable,
        "freshness": rc._requirement_unit_freshness,
        "background": rc._ensure_background_extraction,
    }
    order: list[str] = []

    def fail(*_args, **_kwargs):
        raise AssertionError("public requirements lookup must not run sync preparation")

    class _Result:
        value = {
            "requirements": [{
                "text": "Semantic processor keeps requirements feature intact.",
                "kind": "explicit",
                "polarity": "positive",
                "strength": "high",
                "source": "user",
                "cwd": "/repo",
            }]
        }

    def local_prepare(**_kwargs):
        order.append("local_prepare")
        return {"success": True, "sync": {"skipped": "local_read"}, "freshness": {"fresh": True}}

    def run_sync(spec, query, ctx):
        order.append("processor")
        return _Result()

    def direct_matches(**_kwargs):
        order.append("direct")
        return []

    rc.prepare_requirements_context = fail
    rc.prepare_requirements_local_read_context = local_prepare
    rc.provisioning.run_sync = run_sync
    saved["direct"] = rc._direct_processed_requirement_matches
    rc._direct_processed_requirement_matches = direct_matches
    rc._ensure_requirements_importable = lambda: None
    rc._requirement_unit_freshness = lambda **_kwargs: {"fresh": True, "unhandled_prompts": 0}
    rc._ensure_background_extraction = lambda: {"running": True}
    try:
        result = rc.get_processed_requirements(query="performance logs rca", cwd="/repo")
    finally:
        rc.prepare_requirements_context = saved["prepare"]
        rc.prepare_requirements_local_read_context = saved["local_prepare"]
        rc.provisioning.run_sync = saved["run_sync"]
        rc._direct_processed_requirement_matches = saved["direct"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._requirement_unit_freshness = saved["freshness"]
        rc._ensure_background_extraction = saved["background"]

    check(order == ["local_prepare", "processor", "direct"], "public get-requirements uses processor after local prep")
    check(result["success"] is True, "public get-requirements succeeds through semantic processor")
    check(result["count"] == 1, "public get-requirements returns processor result")
    check(result["requirements"][0]["text"].startswith("Semantic processor"), "semantic processor result is returned")
    check("rg_args" not in result, "public result does not expose raw rg args")
    check("command" not in result, "public result does not expose command")


def test_processor_timeout_response_uses_direct_requirement_matches() -> None:
    import requirement_context as rc

    saved = rc._direct_processed_requirement_matches
    rc._direct_processed_requirement_matches = lambda **_kwargs: [{
        "text": "Direct matches keep get-requirements responsive under processor saturation.",
        "kind": "explicit",
        "polarity": "positive",
        "strength": "high",
        "source": "user",
        "cwd": "/repo",
    }]
    try:
        result = rc.build_processed_requirements_response(
            query="processor saturation",
            cwd="/repo",
            processed={
                "requirements": [],
                "error": (
                    "processor_failed: get-requirements processor timed out before returning "
                    "requirements; no retry attempted"
                ),
            },
        )
    finally:
        rc._direct_processed_requirement_matches = saved

    check(result["success"] is True, "processor timeout can be satisfied by direct requirements")
    check(result["count"] == 1, "direct requirements are returned after processor timeout")
    check("error" not in result, "direct requirements clear processor timeout error")


def test_processor_readtimeout_response_uses_direct_requirement_matches() -> None:
    import requirement_context as rc

    saved = rc._direct_processed_requirement_matches
    rc._direct_processed_requirement_matches = lambda **_kwargs: [{
        "text": "Direct matches keep get-requirements responsive after ReadTimeout.",
        "kind": "explicit",
        "polarity": "positive",
        "strength": "high",
        "source": "user",
        "cwd": "/repo",
    }]
    try:
        result = rc.build_processed_requirements_response(
            query="processor saturation",
            cwd="/repo",
            processed={
                "requirements": [],
                "error": "processor_failed: ReadTimeout",
            },
        )
    finally:
        rc._direct_processed_requirement_matches = saved

    check(result["success"] is True, "processor ReadTimeout can be satisfied by direct requirements")
    check(result["count"] == 1, "direct requirements are returned after processor ReadTimeout")
    check("error" not in result, "direct requirements clear processor ReadTimeout error")


def test_direct_fallback_endpoint_logic_skips_processor() -> None:
    import requirement_context as rc

    saved = {
        "local_prepare": rc.prepare_requirements_local_read_context,
        "processor": rc._run_requirements_processor,
        "direct": rc._direct_processed_requirement_matches,
    }
    calls: list[str] = []

    def fail_processor(**_kwargs):
        raise AssertionError("direct fallback must not dispatch the processor")

    def direct_matches(**kwargs):
        calls.append(f"direct:{kwargs['query']}:{kwargs['max_matches']}")
        return [{
            "text": "Direct fallback returns known requirements after public MCP timeout.",
            "kind": "explicit",
            "polarity": "positive",
            "strength": "high",
            "source": "user",
            "cwd": kwargs["cwd"],
        }]

    rc.prepare_requirements_local_read_context = lambda **_kwargs: calls.append("prepare") or {"success": True}
    rc._run_requirements_processor = fail_processor
    rc._direct_processed_requirement_matches = direct_matches
    try:
        result = rc.get_processed_requirements_direct_fallback(
            query="public timeout",
            cwd="/repo",
            max_matches=3,
        )
    finally:
        rc.prepare_requirements_local_read_context = saved["local_prepare"]
        rc._run_requirements_processor = saved["processor"]
        rc._direct_processed_requirement_matches = saved["direct"]

    check(calls == ["prepare", "direct:public timeout:3"], "direct fallback uses local prep and direct matches only")
    check(result["success"] is True, "direct fallback succeeds when direct matches exist")
    check(result["count"] == 1, "direct fallback returns direct requirements")


def test_mcp_timeout_uses_backend_direct_fallback_endpoint() -> None:
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_test", PKG_ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    calls: list[tuple[str, dict, float]] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append((path, dict(body or {}), timeout))
            if path == "/api/internal/get-requirements":
                raise TimeoutError("timed out")
            if path == "/api/internal/get-requirements/direct-fallback":
                return {
                    "success": True,
                    "requirements": [{
                        "text": "Backend direct fallback satisfies public get-requirements timeout.",
                        "kind": "explicit",
                        "polarity": "positive",
                        "strength": "high",
                        "source": "user",
                        "cwd": "/repo",
                    }],
                    "count": 1,
                }
            raise AssertionError(path)

    saved_client = module.Client
    saved_spill = module.spill_large_result
    module.Client = FakeClient
    module.spill_large_result = lambda result, *, label: result
    try:
        result = module.get_requirements_response(
            " public timeout ",
            cwd="/repo",
            cwds=["/repo/a"],
            all_projects=True,
            max_matches=4,
        )
    finally:
        module.Client = saved_client
        module.spill_large_result = saved_spill

    check(result["success"] is True, "MCP timeout falls back to backend direct endpoint")
    check([call[0] for call in calls] == [
        "/api/internal/get-requirements",
        "/api/internal/get-requirements/direct-fallback",
    ], "MCP calls direct fallback only after public timeout")
    check(calls[1][1] == {
        "query": "public timeout",
        "cwd": "/repo",
        "cwds": ["/repo/a"],
        "all_projects": True,
        "max_matches": 4,
    }, "MCP direct fallback preserves public payload")


def test_mcp_timeout_result_uses_backend_direct_fallback_endpoint() -> None:
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_timeout_result_test", PKG_ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    calls: list[str] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append(path)
            if path == "/api/internal/get-requirements":
                return {
                    "success": False,
                    "requirements": [],
                    "count": 0,
                    "error": (
                        "processor_failed: get-requirements processor timed out before "
                        "returning requirements; no retry attempted"
                    ),
                }
            if path == "/api/internal/get-requirements/direct-fallback":
                return {
                    "success": True,
                    "requirements": [{
                        "text": "Backend direct fallback satisfies timeout result.",
                        "kind": "explicit",
                        "polarity": "positive",
                        "strength": "high",
                        "source": "user",
                        "cwd": "/repo",
                    }],
                    "count": 1,
                }
            raise AssertionError(path)

    saved_client = module.Client
    saved_spill = module.spill_large_result
    module.Client = FakeClient
    module.spill_large_result = lambda result, *, label: result
    try:
        result = module.get_requirements_response("public timeout result", cwd="/repo")
    finally:
        module.Client = saved_client
        module.spill_large_result = saved_spill

    check(result["success"] is True, "MCP timeout result falls back to backend direct endpoint")
    check([call for call in calls] == [
        "/api/internal/get-requirements",
        "/api/internal/get-requirements/direct-fallback",
    ], "MCP calls direct fallback after timeout result")


def test_mcp_non_timeout_does_not_use_direct_fallback() -> None:
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_non_timeout_test", PKG_ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    calls: list[str] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append(path)
            raise RuntimeError("backend unavailable")

    saved_client = module.Client
    module.Client = FakeClient
    try:
        result = module.get_requirements_response("public failure", cwd="/repo")
    finally:
        module.Client = saved_client

    check(result["success"] is False, "MCP non-timeout transport failure still fails")
    check(calls == ["/api/internal/get-requirements"], "MCP does not fallback for non-timeout failures")


def test_raw_search_keeps_processor_off_sync_path() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_context,
        "local_prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
    }
    order: list[str] = []

    def fail(*_args, **_kwargs):
        raise AssertionError("raw requirements lookup must not run sync preparation")

    def local_prepare(**_kwargs):
        order.append("local_prepare")
        return {
            "success": True,
            "sync": {"success": True, "changed": False, "skipped": "local_read"},
            "freshness": {"fresh": True},
            "extraction": {"running": True},
        }

    rc.prepare_requirements_context = fail
    rc.prepare_requirements_local_read_context = local_prepare
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = lambda: [{
        "source_key": "s:1:unit:0",
        "text": "Raw processor lookup stays responsive.",
        "kind": "explicit",
        "source": "user",
        "cwd": "/repo",
    }]
    rc._run_rg = lambda path, args: {
        "command": ["rg", *args, str(path)],
        "returncode": 0,
        "stdout": "1:Raw processor lookup stays responsive.\n",
        "stderr": "",
    }
    try:
        result = rc.search_requirements(
            rg_args=["responsive"],
            cwd="/repo",
            include_unprocessed_prompts=False,
            provider_native_only=False,
            max_matches=5,
        )
    finally:
        rc.prepare_requirements_context = saved["prepare"]
        rc.prepare_requirements_local_read_context = saved["local_prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]

    check(order == ["local_prepare"], "raw search uses local prep only")
    check(result["success"] is True, "raw search succeeds through local prep")
    check(result["count"] == 1, "raw search returns matching unit")


def test_provider_native_only_search_skips_unit_corpus() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
        "native": rc._search_native_transcript_bundles,
    }
    calls: list[str] = []

    def fail(*_args, **_kwargs):
        raise AssertionError("provider-native search must not touch processed requirement units")

    def native(**kwargs):
        calls.append("native")
        check(kwargs["enabled"] is True, "provider-native search enables native transcript bundles")
        check(kwargs["remaining"] == 5, "provider-native search respects max_matches")
        return {
            "enabled": True,
            "searched": True,
            "matches": [{
                "source_key": "native:/repo:1",
                "text": "User approved provider-native requirement extraction.",
                "kind": "native_transcript_bundle",
                "source": "provider_native_transcript",
                "cwd": "/repo",
                "ts": "2026-01-01T00:00:00Z",
            }],
            "count": 1,
            "query": "provider native",
            "index": {"ready": True},
        }

    rc.prepare_requirements_local_read_context = fail
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = fail
    rc._run_rg = fail
    rc._search_native_transcript_bundles = native
    try:
        result = rc.search_requirements(
            rg_args=["-i", "-e", "provider native"],
            cwd="/repo",
            provider_native_only=True,
            max_matches=5,
        )
    finally:
        rc.prepare_requirements_local_read_context = saved["prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]
        rc._search_native_transcript_bundles = saved["native"]

    check(calls == ["native"], "provider-native search uses only native transcript bundles")
    check(result["success"] is True, "provider-native search succeeds")
    check(result["authority"] == "provider_native_transcript_corpus", "provider-native search declares corpus authority")
    check(result["count"] == 1, "provider-native search returns native evidence")


def test_search_defaults_to_provider_native_corpus() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
        "native": rc._search_native_transcript_bundles,
    }

    def fail(*_args, **_kwargs):
        raise AssertionError("default search must not touch the legacy mined-unit path")

    rc.prepare_requirements_local_read_context = fail
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = fail
    rc._run_rg = fail
    rc._search_native_transcript_bundles = lambda **kwargs: {
        "enabled": True, "searched": True, "matches": [], "count": 0,
    }
    try:
        result = rc.search_requirements(rg_args=["-i", "-e", "anything"], cwd="/repo")
    finally:
        rc.prepare_requirements_local_read_context = saved["prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]
        rc._search_native_transcript_bundles = saved["native"]

    check(result["success"] is True, "default search succeeds on native path")
    check(result["authority"] == "provider_native_transcript_corpus",
          "search defaults to the provider-native corpus")


def test_compare_mode_runs_both_paths_and_diffs() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
        "native": rc._search_native_transcript_bundles,
    }
    calls: list[str] = []

    def local_prepare(**_kwargs):
        calls.append("legacy_prepare")
        return {
            "success": True,
            "sync": {"success": True, "changed": False, "skipped": "local_read"},
            "freshness": {"fresh": True},
            "extraction": {"running": True},
        }

    def native(**kwargs):
        if not kwargs.get("enabled"):
            return {"enabled": False, "searched": False, "matches": [], "count": 0}
        calls.append("native")
        return {
            "enabled": True,
            "searched": True,
            "matches": [{
                "text": "Native-only requirement.",
                "kind": "native_transcript_bundle",
                "source": "provider_native_transcript",
                "cwd": "/repo",
                "ts": "2026-02-02T00:00:00Z",
            }],
            "count": 1,
        }

    rc.prepare_requirements_local_read_context = local_prepare
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = lambda: [{
        "source_key": "s:1:unit:0",
        "text": "Legacy-only requirement.",
        "kind": "explicit",
        "source": "user",
        "cwd": "/repo",
        "ts": "2026-01-01T00:00:00Z",
    }]
    rc._run_rg = lambda path, args: {
        "command": ["rg", *args, str(path)],
        "returncode": 0,
        "stdout": "1:Legacy-only requirement.\n",
        "stderr": "",
    }
    rc._search_native_transcript_bundles = native
    try:
        result = rc.search_requirements(
            rg_args=["-i", "-e", "requirement"],
            cwd="/repo",
            compare=True,
            max_matches=5,
        )
    finally:
        rc.prepare_requirements_local_read_context = saved["prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]
        rc._search_native_transcript_bundles = saved["native"]

    check(result.get("compare") is True, "compare mode flags its result")
    check(calls == ["native", "legacy_prepare"], "compare mode runs native then legacy")
    check(result["native"]["authority"] == "provider_native_transcript_corpus",
          "compare mode carries the full native result")
    check(result["legacy"]["count"] == 1, "compare mode carries the full legacy result")
    diff = result["diff"]
    check(diff["native_count"] == 1 and diff["legacy_count"] == 1, "compare diff reports per-side counts")
    check(diff["identical_match_count"] == 0, "compare diff detects disjoint matches")
    check(diff["native_only_ts"] == ["2026-02-02T00:00:00Z"], "compare diff lists native-only timestamps")
    check(diff["legacy_only_ts"] == ["2026-01-01T00:00:00Z"], "compare diff lists legacy-only timestamps")


def test_processor_prompt_is_available_to_running_backend() -> None:
    import requirement_context as rc

    saved = rc._ensure_requirements_importable
    rc._ensure_requirements_importable = lambda: PKG_ROOT
    try:
        prompt = rc.GET_REQUIREMENTS_PROCESSOR_SPEC.build_provision_prompt({})
        instructions = rc.GET_REQUIREMENTS_PROCESSOR_SPEC.build_instructions("session search parse_failed", {
            "cwd": "/repo",
            "cwds": [],
            "all_projects": False,
            "max_matches": 5,
        })
    finally:
        rc._ensure_requirements_importable = saved

    check(prompt.startswith("<get-requirements-processor-prep>"), "processor prompt is available")
    check("Do not call the get-requirements skill" in prompt, "processor prompt forbids recursive public lookup")
    check("provider-native is the default" in prompt, "processor prompt uses provider-native corpus by default")
    check("provider_native_only=False" in prompt, "processor prompt allows legacy fallback only on empty native results")
    check("never pass file paths" in prompt, "processor prompt forbids rg path args")
    check("Do not pass bare token lists" in prompt, "processor prompt rejects bare token rg args")
    check("do not require every term to match" in prompt, "processor prompt preserves partial semantic matches")
    check("kind=native_transcript_bundle" in prompt, "processor prompt explains native transcript bundles")
    check("confirms, adopts, or refines" in prompt, "processor prompt requires user confirmation for native bundles")
    check("Do not call the get-requirements skill" in instructions, "processor instructions forbid recursive public lookup")
    check("provider-native is the default" in instructions, "processor instructions use provider-native corpus by default")
    check("provider_native_only=False" in instructions, "processor instructions allow legacy fallback only on empty native results")
    check("never file paths" in instructions, "processor instructions forbid rg path args")
    check("do not pass bare token lists" in instructions, "processor instructions reject bare token rg args")
    check("do not require every term to match" in instructions, "processor instructions preserve partial semantic matches")
    check("kind=native_transcript_bundle" in instructions, "processor instructions explain native transcript bundles")
    check("confirms, adopts, or refines" in instructions, "processor instructions require user confirmation for native bundles")


def test_processor_dispatch_is_isolated_and_timeout_budgeted() -> None:
    import requirement_context as rc

    saved = rc._ensure_requirements_importable
    rc._ensure_requirements_importable = lambda: PKG_ROOT
    try:
        spec = rc.GET_REQUIREMENTS_PROCESSOR_SPEC
        version = spec.version
        run_mode = spec.run_mode
        ephemeral_forks = spec.ephemeral_forks
    finally:
        rc._ensure_requirements_importable = saved
    server = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")

    check(version >= 3, "processor spec version invalidates stale processor prompt and parser bases")
    check(run_mode == "fork", "processor uses fork mode for lookup isolation")
    check(ephemeral_forks is True, "processor uses ephemeral fork per lookup")
    check("_GET_REQUIREMENTS_TIMEOUT = 105.0" in server, "MCP get-requirements timeout fits provider tool ceiling")
    check("_SEARCH_TIMEOUT = 120.0" in server, "raw search keeps bounded timeout")


def test_processor_spec_fails_closed_without_private_registration() -> None:
    import requirement_context as rc

    saved_get = rc.provisioning.get
    saved_importable = rc._ensure_requirements_importable

    def missing_get(key):
        raise KeyError(key)

    def unavailable():
        raise RuntimeError("extension is not active")

    rc.provisioning.get = missing_get
    rc._ensure_requirements_importable = unavailable
    try:
        result = rc._run_requirements_processor(query="missing private", cwd="/repo")
    finally:
        rc.provisioning.get = saved_get
        rc._ensure_requirements_importable = saved_importable

    check(result["requirements"] == [], "missing private processor returns no requirements")
    check(result["error"].startswith("processor_failed: RuntimeError: provisioned spec"),
          "missing private processor fails closed through processor_failed")


def test_prepare_orchestration_is_cheap_and_nonblocking() -> None:
    import requirement_context as rc

    saved = {k: getattr(rc, k) for k in
             ("_ensure_requirements_importable", "_refresh_user_prompts",
              "_requirement_unit_freshness", "_ensure_background_extraction")}
    calls = {"refresh": 0, "bg": 0}
    try:
        rc._ensure_requirements_importable = lambda: None
        rc._refresh_user_prompts = lambda: (calls.__setitem__("refresh", calls["refresh"] + 1), {"success": True})[1]
        rc._requirement_unit_freshness = lambda **k: {"fresh": False}
        rc._ensure_background_extraction = lambda: (calls.__setitem__("bg", calls["bg"] + 1), {"running": True})[1]

        out = rc.prepare_requirements_context()
        check(calls["refresh"] == 1, "user_prompts synced inline (cheap)")
        check(calls["bg"] == 1, "background extraction ensured once")
        check(set(out) >= {"sync", "freshness", "extraction"}, "prepare returns sync+freshness+extraction")
    finally:
        for k, v in saved.items():
            setattr(rc, k, v)


def test_ensure_background_injects_paths_and_swallows_already_running() -> None:
    import requirement_context as rc
    from requirement_analysis import cli

    saved_root = rc._requirements_package_root
    saved_launch = cli.launch_background
    captured: dict = {}
    try:
        rc._requirements_package_root = lambda: Path("/pkg/root")
        cli.launch_background = lambda argv, *, extra_pythonpath=None: captured.update(
            argv=argv, extra_pythonpath=extra_pythonpath
        ) or {"pid": 1}

        rc._ensure_background_extraction()
        check(captured.get("argv") in (["--extract", "--background"], None), "launches --extract --background")
        pp = captured.get("extra_pythonpath") or []
        check(not captured or (str(Path("/pkg/root")) in pp and str(ROOT) in pp),
              "injects package root + backend dir into child PYTHONPATH")

        def _raise(*a, **k):
            raise RuntimeError("requirement analysis already running: pid=999 phase=prephase")
        cli.launch_background = _raise
        res = rc._ensure_background_extraction()
        check(res.get("running") is True, "already-running guard is swallowed, not raised")
    finally:
        rc._requirements_package_root = saved_root
        cli.launch_background = saved_launch


def test_launch_env_child_can_import_with_injected_paths() -> None:
    # The real failure mode: the detached child's interpreter lacks the package
    # and backend modules on sys.path. Prove the injected roots are sufficient.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(PKG_ROOT), str(ROOT)])
    proc = subprocess.run(
        [sys.executable, "-c", "import requirement_analysis.cli, portable_lock, requirement_context"],
        env=env, cwd=str(TMP_HOME), capture_output=True, text=True,
    )
    check(proc.returncode == 0,
          f"child imports cli+portable_lock+requirement_context with injected PYTHONPATH (stderr: {proc.stderr[-300:]})")


def test_processor_tool_forces_unprocessed_prompts() -> None:
    src = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    fn = src.split("def get_requirements_internal", 1)[1].split("def ", 1)[0]
    check("include_unprocessed_prompts=True" in fn,
          "get_requirements_internal forces include_unprocessed_prompts=True")
    check("include_unprocessed_prompts: bool" not in fn,
          "the LLM-controllable include_unprocessed_prompts param is dropped (deterministic)")
    check("provider_native_only: bool = True" in fn,
          "get_requirements_internal defaults to the provider-native corpus")
    check("provider_native_only=provider_native_only" in fn,
          "get_requirements_internal forwards provider_native_only")
    check("compare: bool = False" in fn,
          "get_requirements_internal exposes manual compare mode, off by default")
    check("compare=compare" in fn,
          "get_requirements_internal forwards compare")


def test_public_tool_guidance_asks_for_task_description() -> None:
    skill = (PKG_ROOT / "skills" / "get-requirements" / "SKILL.md").read_text(encoding="utf-8")
    server = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    public_fn = server.split("def get_requirements(", 1)[1].split("def get_requirements_internal", 1)[0]

    check("task you are about to start" in skill,
          "get-requirements skill asks callers for the task they are about to start")
    check("not generic search keywords" in skill,
          "get-requirements skill rejects generic keyword queries")
    check("concrete task the caller is about to start" in public_fn,
          "public MCP description asks for the concrete task")


def test_direct_fallback_searches_units_and_excludes_raw_bundles() -> None:
    import requirement_context as rc

    saved = {
        "search": rc.search_requirements,
        "prepare": rc.prepare_requirements_local_read_context,
    }
    seen_kwargs: list[dict] = []

    def fake_search(**kwargs):
        seen_kwargs.append(kwargs)
        return {
            "success": True,
            "matches": [
                {
                    "text": "Real mined requirement statement.",
                    "kind": "explicit",
                    "polarity": "positive",
                    "strength": "strong",
                    "source": "user",
                    "cwd": "/repo",
                },
                {
                    "text": "Native transcript evidence bundle.\n[12 assistant tool_call] exec_command ...",
                    "kind": "native_transcript_bundle",
                    "source": "native_transcript",
                    "cwd": "/repo",
                },
            ],
        }

    rc.search_requirements = fake_search
    rc.prepare_requirements_local_read_context = lambda **_kw: {"success": True}
    try:
        response = rc.get_processed_requirements_direct_fallback(query="tool results routing")
    finally:
        rc.search_requirements = saved["search"]
        rc.prepare_requirements_local_read_context = saved["prepare"]

    check(seen_kwargs and seen_kwargs[0].get("provider_native_only") is False,
          "direct fallback searches the mined-unit corpus, not raw native bundles")
    texts = [r.get("text") for r in response["requirements"]]
    check("Real mined requirement statement." in texts, "direct fallback returns unit statements")
    check(all("evidence bundle" not in (t or "") for t in texts),
          "raw transcript bundles never masquerade as processed requirements")
    check(response["success"] is True, "unit-backed fallback reports success")


def test_direct_fallback_keeps_error_when_only_bundles_match() -> None:
    import requirement_context as rc

    saved = {
        "search": rc.search_requirements,
        "prepare": rc.prepare_requirements_local_read_context,
    }

    def bundles_only_search(**kwargs):
        return {
            "success": True,
            "matches": [{
                "text": "Native transcript evidence bundle.",
                "kind": "native_transcript_bundle",
                "source": "native_transcript",
                "cwd": "/repo",
            }],
        }

    rc.search_requirements = bundles_only_search
    rc.prepare_requirements_local_read_context = lambda **_kw: {"success": True}
    try:
        response = rc.get_processed_requirements_direct_fallback(query="tool results routing")
    finally:
        rc.search_requirements = saved["search"]
        rc.prepare_requirements_local_read_context = saved["prepare"]

    check(response["success"] is False, "bundle-only fallback does not mask the processor failure")
    check("timed out" in (response.get("error") or ""), "processor timeout error survives")
    check(response["requirements"] == [], "bundle-only fallback returns no requirements")


def test_native_bundle_sql_retries_once_on_cold_interrupt() -> None:
    import native_transcript_index as idx
    import requirement_context as rc

    saved = idx.run_readonly_sql
    calls: list[float | None] = []

    def flaky_run(sql, params=(), *, row_limit=200, timeout_s=None):
        calls.append(timeout_s)
        if len(calls) == 1:
            return {"error": "OperationalError: interrupted", "columns": [], "rows": []}
        return {
            "columns": ["hit_index", "text", "path", "element_index"],
            "rows": [[1, "warm retry row", "/p.jsonl", 1]],
            "truncated": False,
            "covered": True,
            "usable": True,
        }

    idx.run_readonly_sql = flaky_run
    try:
        rows = rc._native_transcript_sql_window_rows(
            idx, tokens=["needle"], cwds=("/repo",), limit=2,
        )
    finally:
        idx.run_readonly_sql = saved

    check(len(calls) == 2, "interrupted first query retries exactly once")
    check(calls[1] == rc.NATIVE_BUNDLE_COLD_RETRY_TIMEOUT_SECONDS,
          "retry uses the longer cold-cache SQL budget")
    check(rows and rows[0]["text"] == "warm retry row", "retry result is returned")

    calls.clear()

    def always_interrupted(sql, params=(), *, row_limit=200, timeout_s=None):
        calls.append(timeout_s)
        return {"error": "OperationalError: interrupted", "columns": [], "rows": []}

    idx.run_readonly_sql = always_interrupted
    try:
        try:
            rc._native_transcript_sql_window_rows(
                idx, tokens=["needle"], cwds=("/repo",), limit=2,
            )
            raised = False
        except RuntimeError:
            raised = True
    finally:
        idx.run_readonly_sql = saved

    check(len(calls) == 2, "persistent interrupt does not retry more than once")
    check(raised, "persistent interrupt still fails loudly after the retry")


def test_native_transcript_bundle_lookup_uses_indexed_rowids() -> None:
    import json

    import native_session_prompt_search as nsp
    import native_transcript_index as idx
    import requirement_context as rc
    from paths import encode_cwd

    scratch = TMP_HOME / "native-bundle"
    claude = scratch / "claude-projects"
    shutil.rmtree(scratch, ignore_errors=True)
    path = claude / encode_cwd("/repo") / "native-bundle-sid.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        json.dumps({
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "nativebundle needle alpha"},
        }),
        json.dumps({
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "assistant", "content": "reply context beta"},
        }),
    ]) + "\n", encoding="utf-8")

    saved_roots = nsp._native_roots
    nsp._native_roots = lambda: [(claude, "claude")]
    idx.reset_for_test()
    try:
        idx.refresh_once()
        result = rc._native_transcript_bundle_records(
            query="nativebundle needle",
            cwds=("/repo",),
            limit=4,
        )
    finally:
        nsp._native_roots = saved_roots
        idx.reset_for_test()

    matches = result.get("matches") or []
    text = matches[0]["text"] if matches else ""
    check(result.get("searched") is True, "native transcript bundle search runs")
    check(len(matches) == 1, "native transcript bundle returns one hit")
    check("nativebundle needle alpha" in text and "reply context beta" in text,
          "native transcript bundle includes hit window")


def test_requirements_query_executors_are_split() -> None:
    """The public processor path re-enters /search via the
    get_requirements_internal MCP tool; sharing one bounded pool between the
    two endpoints self-deadlocks under >=2 concurrent public calls. The
    processor (reentrant, long-running) and search (leaf) paths must use
    distinct pools so a processor worker never waits on a slot it holds."""
    from concurrent.futures import ThreadPoolExecutor

    import requirements_query_runner as runner

    check(
        isinstance(runner.REQUIREMENTS_PROCESSOR_EXECUTOR, ThreadPoolExecutor),
        "processor executor is a ThreadPoolExecutor",
    )
    check(
        isinstance(runner.REQUIREMENTS_SEARCH_EXECUTOR, ThreadPoolExecutor),
        "search executor is a ThreadPoolExecutor",
    )
    check(
        runner.REQUIREMENTS_PROCESSOR_EXECUTOR is not runner.REQUIREMENTS_SEARCH_EXECUTOR,
        "processor and search endpoints use distinct pools (no self-deadlock)",
    )


def test_reentrant_search_does_not_deadlock() -> None:
    """Behavioral lock: a processor task on the processor pool that nests a
    /search on the search pool completes for several concurrent processors.
    Under the old single-pool design two processors would saturate the pool
    and the nested search would starve forever."""
    import asyncio

    import requirements_query_runner as runner

    async def _processor_like(idx: int) -> str:
        async def _search_part() -> str:
            return await runner.run_requirements_query(
                f"search.{idx}",
                lambda: f"search-{idx}",
                executor=runner.REQUIREMENTS_SEARCH_EXECUTOR,
            )

        def _processor_work() -> str:
            # The processor runs sync and waits for its nested /search result,
            # exactly like provisioning.run_sync blocks the processor worker.
            return asyncio.run(_search_part())

        return await runner.run_requirements_query(
            f"processed.{idx}",
            _processor_work,
            executor=runner.REQUIREMENTS_PROCESSOR_EXECUTOR,
        )

    async def _main() -> list[str]:
        return await asyncio.gather(*(_processor_like(i) for i in range(4)))

    try:
        results = asyncio.run(asyncio.wait_for(_main(), timeout=10.0))
        ok = sorted(results) == [f"search-{i}" for i in range(4)]
    except asyncio.TimeoutError:
        ok = False
    check(ok, "reentrant processor->search completes without pool self-deadlock")


def run() -> None:
    test_greedy_packing_respects_capacity_and_cap()
    test_milp_failure_falls_back_to_greedy()
    test_query_path_has_no_inline_extraction()
    test_requirements_query_executors_are_split()
    test_reentrant_search_does_not_deadlock()
    test_public_get_requirements_keeps_processor_off_sync_path()
    test_processor_timeout_response_uses_direct_requirement_matches()
    test_processor_readtimeout_response_uses_direct_requirement_matches()
    test_direct_fallback_endpoint_logic_skips_processor()
    test_mcp_timeout_uses_backend_direct_fallback_endpoint()
    test_mcp_timeout_result_uses_backend_direct_fallback_endpoint()
    test_mcp_non_timeout_does_not_use_direct_fallback()
    test_raw_search_keeps_processor_off_sync_path()
    test_provider_native_only_search_skips_unit_corpus()
    test_search_defaults_to_provider_native_corpus()
    test_compare_mode_runs_both_paths_and_diffs()
    test_processor_prompt_is_available_to_running_backend()
    test_processor_dispatch_is_isolated_and_timeout_budgeted()
    test_processor_spec_fails_closed_without_private_registration()
    test_prepare_orchestration_is_cheap_and_nonblocking()
    test_ensure_background_injects_paths_and_swallows_already_running()
    test_launch_env_child_can_import_with_injected_paths()
    test_processor_tool_forces_unprocessed_prompts()
    test_public_tool_guidance_asks_for_task_description()
    test_direct_fallback_searches_units_and_excludes_raw_bundles()
    test_direct_fallback_keeps_error_when_only_bundles_match()
    test_native_bundle_sql_retries_once_on_cold_interrupt()
    test_native_transcript_bundle_lookup_uses_indexed_rowids()


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        sys.exit(1)
    print("\nALL PASSED")

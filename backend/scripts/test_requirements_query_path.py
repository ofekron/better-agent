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

import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-req-query-path-"))
import _test_home
_test_home.isolate("ba-test-")

ROOT = Path(__file__).resolve().parents[1]                       # .../backend
REPO = ROOT.parent
PKG_ROOT = REPO / "better-agent-private" / "extensions" / "requirements"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PKG_ROOT))

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

    rc.prepare_requirements_context = fail
    rc.prepare_requirements_local_read_context = local_prepare
    rc.provisioning.run_sync = run_sync
    rc._ensure_requirements_importable = lambda: None
    rc._requirement_unit_freshness = lambda **_kwargs: {"fresh": True, "unhandled_prompts": 0}
    rc._ensure_background_extraction = lambda: {"running": True}
    try:
        result = rc.get_processed_requirements(query="performance logs rca", cwd="/repo")
    finally:
        rc.prepare_requirements_context = saved["prepare"]
        rc.prepare_requirements_local_read_context = saved["local_prepare"]
        rc.provisioning.run_sync = saved["run_sync"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._requirement_unit_freshness = saved["freshness"]
        rc._ensure_background_extraction = saved["background"]

    check(order == ["local_prepare", "processor"], "public get-requirements uses processor after local prep")
    check(result["success"] is True, "public get-requirements succeeds through semantic processor")
    check(result["count"] == 1, "public get-requirements returns processor result")
    check(result["requirements"][0]["text"].startswith("Semantic processor"), "semantic processor result is returned")
    check("rg_args" not in result, "public result does not expose raw rg args")
    check("command" not in result, "public result does not expose command")


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
    check("never pass file paths" in prompt, "processor prompt forbids rg path args")
    check("Do not pass bare token lists" in prompt, "processor prompt rejects bare token rg args")
    check("do not require every term to match" in prompt, "processor prompt preserves partial semantic matches")
    check("kind=native_transcript_bundle" in prompt, "processor prompt explains native transcript bundles")
    check("confirms, adopts, or refines" in prompt, "processor prompt requires user confirmation for native bundles")
    check("Do not call the get-requirements skill" in instructions, "processor instructions forbid recursive public lookup")
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
    check("_GET_REQUIREMENTS_TIMEOUT = 330.0" in server, "MCP get-requirements timeout covers three processor attempts")
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
        check(captured.get("argv") == ["--extract", "--background"], "launches --extract --background")
        pp = captured.get("extra_pythonpath") or []
        check(str(Path("/pkg/root")) in pp and str(ROOT) in pp,
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


def run() -> None:
    test_greedy_packing_respects_capacity_and_cap()
    test_milp_failure_falls_back_to_greedy()
    test_query_path_has_no_inline_extraction()
    test_public_get_requirements_keeps_processor_off_sync_path()
    test_raw_search_keeps_processor_off_sync_path()
    test_processor_prompt_is_available_to_running_backend()
    test_processor_dispatch_is_isolated_and_timeout_budgeted()
    test_processor_spec_fails_closed_without_private_registration()
    test_prepare_orchestration_is_cheap_and_nonblocking()
    test_ensure_background_injects_paths_and_swallows_already_running()
    test_launch_env_child_can_import_with_injected_paths()
    test_processor_tool_forces_unprocessed_prompts()


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        sys.exit(1)
    print("\nALL PASSED")

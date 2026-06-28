#!/usr/bin/env python3
"""Locks the requirements execution-model redesign:

1. get-requirements query path is cheap + non-blocking: it syncs user_prompts
   and ensures the detached background runner is alive, but NEVER runs unit
   extraction or the downstream DAG inline.
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


def test_public_get_requirements_is_local_read_only() -> None:
    import requirement_context as rc
    from requirement_analysis.prephase import prephase_batches_path, units_path

    saved = {
        "prepare": rc.prepare_requirements_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "freshness": rc._requirement_unit_freshness,
        "background": rc._ensure_background_extraction,
    }
    units_path().parent.mkdir(parents=True, exist_ok=True)
    units_path().write_text(
        '{"source_key":"s1:1:unit:0","text":"Performance logs must drive RCA before fixes.",'
        '"kind":"explicit","polarity":"positive","strength":"high","source":"user","cwd":"/repo"}\n',
        encoding="utf-8",
    )
    prephase_batches_path().write_text(
        '{"version":1,"handled_session_counts":{},"batched_session_counts":{},"batches":[]}',
        encoding="utf-8",
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("public requirements lookup must not call sync or worker")

    rc.prepare_requirements_context = fail
    rc._ensure_requirements_importable = lambda: None
    rc._requirement_unit_freshness = lambda **_kwargs: {"fresh": True, "unhandled_prompts": 0}
    rc._ensure_background_extraction = lambda: {"running": True}
    try:
        result = rc.get_processed_requirements(query="performance logs rca", cwd="/repo")
    finally:
        rc.prepare_requirements_context = saved["prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._requirement_unit_freshness = saved["freshness"]
        rc._ensure_background_extraction = saved["background"]

    check(result["success"] is True, "public get-requirements succeeds from local corpus")
    check(result["count"] == 1, "public get-requirements returns local matches")
    check(result["requirements"][0]["text"].startswith("Performance logs"), "public result returns requirement text")
    check("rg_args" not in result, "public result does not expose raw rg args")
    check("command" not in result, "public result does not expose command")


def test_public_get_requirements_ranks_noisy_queries() -> None:
    import requirement_context as rc
    from requirement_analysis.prephase import prephase_batches_path, units_path

    saved = {
        "ensure_importable": rc._ensure_requirements_importable,
        "freshness": rc._requirement_unit_freshness,
        "background": rc._ensure_background_extraction,
    }
    units_path().parent.mkdir(parents=True, exist_ok=True)
    units_path().write_text(
        "\n".join([
            '{"source_key":"s1:1:unit:0","text":"Auth settings must be backend-owned.",'
            '"kind":"explicit","source":"user","cwd":"/repo"}',
            '{"source_key":"s2:1:unit:0","text":"Generic work items should stay concise.",'
            '"kind":"explicit","source":"user","cwd":"/repo"}',
        ]) + "\n",
        encoding="utf-8",
    )
    prephase_batches_path().write_text(
        '{"version":1,"handled_session_counts":{},"batched_session_counts":{},"batches":[]}',
        encoding="utf-8",
    )

    rc._ensure_requirements_importable = lambda: None
    rc._requirement_unit_freshness = lambda **_kwargs: {"fresh": True, "unhandled_prompts": 0}
    rc._ensure_background_extraction = lambda: {"running": True}
    try:
        result = rc.get_processed_requirements(query="how does auth work", cwd="/repo", max_matches=2)
    finally:
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._requirement_unit_freshness = saved["freshness"]
        rc._ensure_background_extraction = saved["background"]

    check(result["success"] is True, "noisy public query succeeds")
    check(result["count"] == 1, "stopword-heavy query avoids generic work-only match")
    check(result["requirements"][0]["text"].startswith("Auth settings"), "most relevant requirement ranks first")


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
    test_public_get_requirements_is_local_read_only()
    test_public_get_requirements_ranks_noisy_queries()
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

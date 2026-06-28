"""Unit tests for stores/task_store.py — durable, project-scoped, reusable
task DEFINITIONS that launch autonomous sessions on demand.

Locks: CRUD, project-scoped listing, validation bounds (name/prompt/cwd
required, mode/policy enums, per-project cap), update re-validation,
run-breadcrumb recording (counter bump + recent-runs cap + singleton
binding), session-reference cleanup, and the loud-empty behavior on
schema-version mismatch.
"""
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-task-store-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from stores import task_store  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def main() -> int:
    print("T1 CRUD + project scoping")
    t1 = task_store.create(cwd="/proj", name="Run tests", prompt="pytest -q")
    check(task_store.get(t1["id"]) is not None, "create+get")
    # Unattended defaults.
    check(t1["orchestration_mode"] == "native", "default mode native")
    check(t1["worker_creation_policy"] == "approve", "default policy approve")
    check(t1["run_count"] == 0 and t1["recent_runs"] == [], "fresh run state")
    task_store.create(cwd="/proj", name="Lint", prompt="ruff check")
    task_store.create(cwd="/other", name="Build", prompt="make")
    check(len(task_store.list_for_project("/proj")) == 2, "list scoped by cwd")
    check(len(task_store.list_for_project("/other")) == 1, "other cwd separate")
    check(task_store.list_for_project("/proj", "node2") == [], "scoped by node")
    removed = task_store.delete(t1["id"])
    check(removed is not None and task_store.get(t1["id"]) is None, "delete")
    check(task_store.delete("nope") is None, "delete unknown → None")

    print("T2 validation bounds")
    bads = [
        dict(cwd="/p", name="", prompt="x"),
        dict(cwd="/p", name="n", prompt=""),
        dict(cwd="", name="n", prompt="x"),
        dict(cwd="/p", name="n", prompt="x", orchestration_mode="weird"),
        dict(cwd="/p", name="n", prompt="x", worker_creation_policy="weird"),
        dict(cwd="/p", name="x" * (task_store.MAX_NAME_LEN + 1), prompt="x"),
        dict(cwd="/p", name="n", prompt="x" * (task_store.MAX_PROMPT_LEN + 1)),
        dict(cwd="/p", name="n", prompt="x", permission="not-a-dict"),
        dict(cwd="/p", name="n", prompt="x", permission={"a": 1}),  # non-str value
    ]
    for bad in bads:
        try:
            task_store.create(**bad)
            check(False, f"accepted invalid: {str(bad)[:60]}")
        except ValueError:
            check(True, f"rejected: {str(bad)[:60]}")

    # manager alias normalizes to team
    tm = task_store.create(cwd="/p", name="m", prompt="x", orchestration_mode="manager")
    check(tm["orchestration_mode"] == "team", "manager → team")

    print("T3 update re-validates")
    upd = task_store.update(tm["id"], {"name": "renamed", "singleton": True})
    check(upd is not None and upd["name"] == "renamed" and upd["singleton"] is True, "update applies")
    try:
        task_store.update(tm["id"], {"name": ""})
        check(False, "update accepted empty name")
    except ValueError:
        check(True, "update rejects empty name")
    check(task_store.update("nope", {"name": "x"}) is None, "update unknown → None")
    # cwd is immutable via update (not in editable fields)
    upd2 = task_store.update(tm["id"], {"cwd": "/evil"})
    check(upd2 is not None and upd2["cwd"] == "/p", "cwd immutable via update")

    print("T4 run breadcrumbs")
    rt = task_store.create(cwd="/proj", name="R", prompt="go", singleton=True)
    task_store.record_run(rt["id"], "sessA")
    rec = task_store.get(rt["id"])
    check(rec["run_count"] == 1, "run_count bumped")
    check(rec["recent_runs"][0]["session_id"] == "sessA", "recent_runs prepended")
    check(rec["last_run_at"] is not None, "last_run_at stamped")
    check(rec["singleton_session_id"] == "sessA", "singleton binding set")
    # recent_runs cap + newest-first + dedup
    for i in range(task_store.MAX_RECENT_RUNS + 3):
        task_store.record_run(rt["id"], f"sess{i}")
    rec = task_store.get(rt["id"])
    check(len(rec["recent_runs"]) == task_store.MAX_RECENT_RUNS, "recent_runs capped")
    # re-running an existing session id doesn't duplicate the entry
    before = len(task_store.get(rt["id"])["recent_runs"])
    task_store.record_run(rt["id"], rec["recent_runs"][0]["session_id"])
    after = len(task_store.get(rt["id"])["recent_runs"])
    check(after == before, "re-run same session de-dups in recent_runs")

    print("T5 session-reference cleanup")
    bound = task_store.get(rt["id"])["singleton_session_id"]
    changed = task_store.drop_session_references(bound)
    check(rt["id"] in changed, "drop_session_references reports changed task")
    rec = task_store.get(rt["id"])
    check(rec["singleton_session_id"] is None, "singleton binding cleared on delete")
    check(all(r["session_id"] != bound for r in rec["recent_runs"]), "breadcrumb removed")
    check(task_store.clear_singleton_session(rt["id"])["singleton_session_id"] is None,
          "clear_singleton_session idempotent")

    print("T6 per-project cap")
    capcwd = "/capproj"
    for i in range(task_store.MAX_PER_PROJECT):
        task_store.create(cwd=capcwd, name=f"t{i}", prompt="x")
    try:
        task_store.create(cwd=capcwd, name="overflow", prompt="x")
        check(False, "cap not enforced")
    except ValueError:
        check(True, f"per-project cap enforced at {task_store.MAX_PER_PROJECT}")

    print("T7 schema-version mismatch → loud empty")
    task_store._path().write_text('{"version": 999, "tasks": [{}]}')
    check(task_store.list_for_project("/proj") == [],
          "bad version reads as empty (wipe to start fresh)")

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: task_store")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

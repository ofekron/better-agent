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
    check(task_store.delete("nope") is None, "delete unknown -> None")

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
        dict(cwd="/p", name="n", prompt="x", permission={"a": 1}),
    ]
    for bad in bads:
        try:
            task_store.create(**bad)
            check(False, f"accepted invalid: {str(bad)[:60]}")
        except ValueError:
            check(True, f"rejected: {str(bad)[:60]}")

    tm = task_store.create(cwd="/p", name="m", prompt="x", orchestration_mode="manager")
    check(tm["orchestration_mode"] == "team", "manager -> team")

    print("T3 update re-validates")
    upd = task_store.update(tm["id"], {"name": "renamed", "singleton": True})
    check(upd is not None and upd["name"] == "renamed" and upd["singleton"] is True, "update applies")
    try:
        task_store.update(tm["id"], {"name": ""})
        check(False, "update accepted empty name")
    except ValueError:
        check(True, "update rejects empty name")
    check(task_store.update("nope", {"name": "x"}) is None, "update unknown -> None")
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
    for i in range(task_store.MAX_RECENT_RUNS + 3):
        task_store.record_run(rt["id"], f"sess{i}")
    rec = task_store.get(rt["id"])
    check(len(rec["recent_runs"]) == task_store.MAX_RECENT_RUNS, "recent_runs capped")
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

    print("T7 schema-version mismatch -> loud empty")
    task_store._path().write_text('{"version": 999, "tasks": [{}]}')
    check(task_store.list_for_project("/proj") == [],
          "bad version reads as empty (wipe to start fresh)")

    print("T8 trigger validation")
    check(task_store._coerce_trigger(None) == {"kind": "manual", "config": {}},
          "no trigger -> manual default")
    try:
        task_store._coerce_trigger({"kind": "bogus"})
        check(False, "bad trigger.kind accepted")
    except ValueError:
        check(True, "bad trigger.kind rejected")
    try:
        task_store._coerce_trigger({"kind": "schedule", "interval_seconds": 300})
        check(False, "schedule fields at top level (not under config) accepted")
    except ValueError as e:
        check("interval_seconds" in str(e) and "config" in str(e),
              f"misplaced schedule fields rejected with actionable message: {e}")
    try:
        task_store._coerce_trigger({"kind": "schedule", "config": {}})
        check(False, "schedule with empty config accepted")
    except ValueError as e:
        check("fire_at" in str(e) and "config" in str(e),
              f"schedule once w/o fire_at names the missing field: {e}")
    try:
        task_store._coerce_trigger({"kind": "schedule", "config": {"mode": "recurring"}})
        check(False, "recurring schedule w/o interval_seconds accepted")
    except ValueError as e:
        check("interval_seconds" in str(e) and "config" in str(e),
              f"schedule recurring w/o interval_seconds names the missing field: {e}")
    once = task_store._coerce_trigger(
        {"kind": "schedule", "config": {"mode": "once", "fire_at": "2026-07-14T15:00:00Z"}})
    check(once == {"kind": "schedule",
                   "config": {"mode": "once", "fire_at": "2026-07-14T15:00:00Z"}},
          "valid once trigger coerces cleanly")
    recurring = task_store._coerce_trigger(
        {"kind": "schedule", "config": {"mode": "recurring", "interval_seconds": 300}})
    check(recurring == {"kind": "schedule",
                         "config": {"mode": "recurring", "interval_seconds": 300}},
          "valid recurring trigger coerces cleanly")
    try:
        task_store._coerce_trigger(
            {"kind": "schedule", "config": {"mode": "recurring", "interval_seconds": 1}})
        check(False, "sub-minimum interval accepted")
    except ValueError:
        check(True, "sub-minimum interval rejected")

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

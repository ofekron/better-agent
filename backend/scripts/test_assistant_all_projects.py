"""Regression guard: `all_projects` sessions appear in every project filter.

The assistant singleton has no project cwd; `session_matches_project` is the
single membership rule and every project_path filter site must route through
it (backend list/facet paths + the frontend mirror in useSession.ts).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-assistant-all-projects-")

import session_store  # noqa: E402
from session_manager import manager, session_matches_project  # noqa: E402


def check(cond: bool, msg: str, failures: list[str]) -> None:
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def main() -> int:
    failures: list[str] = []

    # ── Membership rule truth table ──────────────────────────────
    plain = {"cwd": "/tmp/projA"}
    flagged = {"cwd": "", "all_projects": True}
    check(session_matches_project(plain, None), "no filter includes plain session", failures)
    check(session_matches_project(plain, "/tmp/projA"), "cwd match includes plain session", failures)
    check(not session_matches_project(plain, "/tmp/projB"), "cwd mismatch excludes plain session", failures)
    check(session_matches_project(flagged, "/tmp/projA"), "all_projects included in projA", failures)
    check(session_matches_project(flagged, "/tmp/projB"), "all_projects included in projB", failures)

    # ── Flag persists on the session record and its summary ─────
    sess = manager.create(name="all-projects-test", orchestration_mode="native", source="extension")
    sid = sess["id"]
    updated = manager.set_all_projects(sid, True)
    check(bool(updated and updated.get("all_projects")), "set_all_projects persists on record", failures)
    summary = session_store._build_summary_for_root(manager.get(sid))
    check(summary.get("all_projects") is True, "summary projection carries all_projects", failures)
    check(
        session_matches_project(summary, "/any/project/path"),
        "flagged summary matches arbitrary project filter",
        failures,
    )

    # ── Every backend filter site routes through the helper ─────
    main_source = (_BACKEND / "main.py").read_text(encoding="utf-8")
    check(
        'summary.get("cwd") != project_path' not in main_source,
        "sidebar page filter uses session_matches_project",
        failures,
    )
    check(
        'session.get("cwd") != project_path' not in main_source,
        "list-filters helper uses session_matches_project",
        failures,
    )
    check(
        'session.get("cwd") != project_id' not in main_source,
        "org facets filter uses session_matches_project",
        failures,
    )

    # ── Assistant singleton gets the flag; frontend mirrors it ──
    assistant_source = (_BACKEND / "assistant_ui.py").read_text(encoding="utf-8")
    check("set_all_projects" in assistant_source, "assistant ensure sets all_projects", failures)
    use_session = (_BACKEND.parent / "frontend" / "src" / "hooks" / "useSession.ts").read_text(encoding="utf-8")
    check("all_projects" in use_session, "useSession mirrors all_projects membership", failures)

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("\nPASS: all_projects sessions visible across projects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

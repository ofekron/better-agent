"""Backend tests for the bidirectional file-panel feature.

Pins the contract for:
  * session.open_file_panels persistence + de-dupe-by-path + reorder
  * user REST routes POST/DELETE/PUT /api/sessions/{id}/file-panels
  * the internal /api/internal/open-file-panel MCP loopback
      - bad token → 403
      - mode=panel → success + state mutated
      - mode=inline → success but state NOT mutated (the persisted
        tool-call event is the source of truth, not session state)
  * session_metadata_updated WS broadcast with originated_by echo
  * new + legacy sessions default open_file_panels to []
  * SCOPING (finding A): only genuine user turns enable the
    open_file_panel MCP tool — native/manager handle_turn pass
    user_initiated=True; supervisor.run_primary_turn does NOT.

Run with:
    cd backend && .venv/bin/python scripts/test_open_file_panels.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

# Per CLAUDE.md, isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module so the dev's real session store is
# never touched.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ofp-")
# The user-facing REST routes under /api/sessions/* require auth.
# The test client gets a normal bearer token below; internal routes
# use X-Internal-Token.

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import session_store  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_session(mode: str = "native") -> str:
    sess = session_store.create_session(
        name="t", model="m", cwd="/tmp", orchestration_mode=mode
    )
    return sess["id"]


# ---------------------------------------------------------------- A
def test_new_session_seeds_empty_panels(client: TestClient) -> bool:
    r = client.post("/api/sessions", json={"name": "x", "cwd": "/tmp"})
    if r.json().get("open_file_panels") != []:
        print(f"  default not []: {r.json().get('open_file_panels')}")
        return False
    return True


def test_legacy_session_migrates_with_empty_panels(client: TestClient) -> bool:
    from pathlib import Path

    legacy = {
        "id": "legacy-ofp",
        "name": "legacy",
        "model": "m",
        "cwd": "/tmp",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "messages": [],
    }
    path = Path(_TMP_HOME) / "sessions" / "legacy-ofp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy))
    sess = session_store.get_session("legacy-ofp")
    if sess.get("open_file_panels") != []:
        print(f"  legacy default missing: {sess.get('open_file_panels')}")
        return False
    return True


def test_post_persists_and_dedupes_by_path(client: TestClient) -> bool:
    sid = _new_session()
    client.post(
        f"/api/sessions/{sid}/file-panels",
        json={"id": "p1", "path": "/tmp/a.py",
              "focus": {"startLine": 1, "endLine": 10}},
    )
    client.post(
        f"/api/sessions/{sid}/file-panels",
        json={"id": "p3", "path": "/tmp/b.py"},
    )
    # Same path again → de-duped, focus updated, moved to the end so
    # tab order stays the single source of truth for latest open focus.
    client.post(
        f"/api/sessions/{sid}/file-panels",
        json={"id": "p2", "path": "/tmp/a.py",
              "focus": {"startLine": 20, "endLine": 30}},
    )
    # Read canonical in-memory backend state (the source of truth the
    # app serves). The disk mirror is leading-edge debounced, so reading
    # session_store directly would race the deferred tail-flush after
    # rapid successive writes.
    panels = session_manager.get(sid)["open_file_panels"]
    if len(panels) != 2:
        print(f"  expected 2 panels (deduped), got {panels}")
        return False
    a = next(p for p in panels if p["path"] == "/tmp/a.py")
    if a["id"] != "p1":
        print(f"  dedupe should preserve existing id: {a}")
        return False
    if a["focus"] != {"startLine": 20, "endLine": 30}:
        print(f"  dedupe should update focus in place: {a}")
        return False
    paths = [p["path"] for p in panels]
    if paths != ["/tmp/b.py", "/tmp/a.py"]:
        print(f"  dedupe should move reopened panel to end: {paths}")
        return False
    return True


def test_delete_removes_panel(client: TestClient) -> bool:
    sid = _new_session()
    client.post(f"/api/sessions/{sid}/file-panels",
                json={"id": "x", "path": "/tmp/x.py"})
    r = client.delete(f"/api/sessions/{sid}/file-panels/x")
    if r.status_code != 200:
        print(f"  delete status {r.status_code}")
        return False
    if session_manager.get(sid)["open_file_panels"] != []:
        print("  panel not removed")
        return False
    return True


def test_put_replaces_and_reorders(client: TestClient) -> bool:
    sid = _new_session()
    client.post(f"/api/sessions/{sid}/file-panels",
                json={"id": "a", "path": "/tmp/a"})
    client.post(f"/api/sessions/{sid}/file-panels",
                json={"id": "b", "path": "/tmp/b"})
    r = client.put(
        f"/api/sessions/{sid}/file-panels",
        json={"panels": [
            {"id": "b", "path": "/tmp/b"},
            {"id": "a", "path": "/tmp/a"},
        ]},
    )
    if r.status_code != 200:
        print(f"  put status {r.status_code}: {r.text}")
        return False
    paths = [p["path"] for p in session_manager.get(sid)["open_file_panels"]]
    if paths != ["/tmp/b", "/tmp/a"]:
        print(f"  reorder failed: {paths}")
        return False
    return True


def test_post_missing_path_is_400(client: TestClient) -> bool:
    sid = _new_session()
    r = client.post(f"/api/sessions/{sid}/file-panels", json={"id": "z"})
    if r.status_code != 400:
        print(f"  expected 400, got {r.status_code}")
        return False
    return True


def test_post_missing_session_is_404(client: TestClient) -> bool:
    r = client.post("/api/sessions/nope/file-panels",
                     json={"path": "/tmp/a"})
    if r.status_code != 404:
        print(f"  expected 404, got {r.status_code}")
        return False
    return True


def test_file_metadata_changes_after_write(client: TestClient) -> bool:
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="bc-file-meta-"))
    try:
        session_store.create_session(
            name="meta", model="m", cwd=str(root), orchestration_mode="native"
        )
        path = root / "a.txt"
        path.write_text("one", encoding="utf-8")
        first = client.get("/api/file/metadata", params={"path": str(path)})
        if first.status_code != 200:
            print(f"  first metadata failed: {first.status_code} {first.text}")
            return False
        first_data = first.json()
        path.write_text("two longer", encoding="utf-8")
        second = client.get("/api/file/metadata", params={"path": str(path)})
        if second.status_code != 200:
            print(f"  second metadata failed: {second.status_code} {second.text}")
            return False
        second_data = second.json()
        if first_data.get("mtime_ns") == second_data.get("mtime_ns"):
            print(f"  mtime did not change: {first_data} -> {second_data}")
            return False
        if first_data.get("size") == second_data.get("size"):
            print(f"  size did not change: {first_data} -> {second_data}")
            return False
        return True
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- B
def _capture_metadata_event(coordinator, session_id: str, action):
    received: list[dict] = []

    async def cb(ev):
        received.append(ev)

    coordinator.register_global_ws(cb)
    try:
        action()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            for ev in received:
                if ev.get("type") == "session_metadata_updated":
                    return ev
            time.sleep(0.01)
    finally:
        coordinator.unregister_global_ws(cb)
    for ev in received:
        if ev.get("type") == "session_metadata_updated":
            return ev
    return None


def test_post_emits_ws_with_origin(client: TestClient) -> bool:
    sid = _new_session()

    def go():
        return client.post(
            f"/api/sessions/{sid}/file-panels",
            json={"id": "p", "path": "/tmp/a.py", "client_id": "tab-a"},
        )

    ev = _capture_metadata_event(main.coordinator, sid, go)
    if not ev:
        print("  no broadcast on file-panel POST")
        return False
    data = ev.get("data") or {}
    if data.get("originated_by") != "tab-a":
        print(f"  originated_by not echoed: {data}")
        return False
    if not isinstance((data.get("patch") or {}).get("open_file_panels"), list):
        print(f"  patch.open_file_panels missing: {data}")
        return False
    return True


def test_delete_emits_ws_with_origin_query(client: TestClient) -> bool:
    sid = _new_session()
    client.post(f"/api/sessions/{sid}/file-panels",
                json={"id": "d", "path": "/tmp/d.py"})

    def go():
        return client.delete(
            f"/api/sessions/{sid}/file-panels/d?client_id=tab-z"
        )

    ev = _capture_metadata_event(main.coordinator, sid, go)
    if not ev or (ev.get("data") or {}).get("originated_by") != "tab-z":
        print(f"  bad/no broadcast on DELETE: {ev}")
        return False
    return True


# ---------------------------------------------------------------- C
def _tok() -> str:
    return main.coordinator.internal_token


def test_internal_bad_token_403(client: TestClient) -> bool:
    sid = _new_session()
    r = client.post(
        "/api/internal/open-file-panel",
        json={"app_session_id": sid, "mode": "panel", "path": "/tmp/a"},
        headers={"X-Internal-Token": "wrong"},
    )
    if r.status_code != 403:
        print(f"  expected 403, got {r.status_code}")
        return False
    return True


def test_internal_panel_mode_mutates(client: TestClient) -> bool:
    sid = _new_session()
    r = client.post(
        "/api/internal/open-file-panel",
        json={"app_session_id": sid, "mode": "panel", "path": "/tmp/a.py",
              "start_line": 5, "end_line": 9,
              "selected_start": 6, "selected_end": 7},
        headers={"X-Internal-Token": _tok()},
    )
    body = r.json()
    if not body.get("success"):
        print(f"  expected success: {body}")
        return False
    panels = session_store.get_session(sid)["open_file_panels"]
    if len(panels) != 1 or panels[0]["path"] != "/tmp/a.py":
        print(f"  panel not persisted: {panels}")
        return False
    if panels[0]["focus"] != {"startLine": 5, "endLine": 9}:
        print(f"  focus wrong: {panels[0]}")
        return False
    if panels[0]["selection"] != {"startLine": 6, "endLine": 7}:
        print(f"  selection wrong: {panels[0]}")
        return False
    return True


def test_internal_inline_mode_does_not_mutate(client: TestClient) -> bool:
    sid = _new_session()
    r = client.post(
        "/api/internal/open-file-panel",
        json={"app_session_id": sid, "mode": "inline", "path": "/tmp/a.py",
              "start_line": 1, "end_line": 3},
        headers={"X-Internal-Token": _tok()},
    )
    body = r.json()
    if not body.get("success"):
        print(f"  inline expected success: {body}")
        return False
    if body.get("mode") != "inline":
        print(f"  inline mode echo wrong: {body}")
        return False
    if session_store.get_session(sid)["open_file_panels"] != []:
        print("  inline mode wrongly mutated session state")
        return False
    return True


def test_internal_relative_path_resolved(client: TestClient) -> bool:
    sess = session_store.create_session(name="t", model="m", cwd="/work/dir")
    r = client.post(
        "/api/internal/open-file-panel",
        json={"app_session_id": sess["id"], "mode": "panel",
              "path": "src/x.py"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.json()["panel"]["path"] != "/work/dir/src/x.py":
        print(f"  relative path not resolved: {r.json()}")
        return False
    return True


def test_internal_missing_session_returns_failure(client: TestClient) -> bool:
    r = client.post(
        "/api/internal/open-file-panel",
        json={"app_session_id": "nope", "mode": "panel", "path": "/a"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.json().get("success") is not False:
        print(f"  expected success False: {r.json()}")
        return False
    return True


def test_internal_bad_mode_returns_failure(client: TestClient) -> bool:
    sid = _new_session()
    r = client.post(
        "/api/internal/open-file-panel",
        json={"app_session_id": sid, "mode": "zzz", "path": "/a"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.json().get("success") is not False:
        print(f"  expected success False for bad mode: {r.json()}")
        return False
    return True


# ---------------------------------------------------------------- E
class _FakeCoord:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.turn_manager = self

    async def run_turn(self, **kw) -> None:
        self.calls.append(kw)

    # The post-turn supervisor tail (maybe_supervise) probes this before
    # its supervisor_enabled early-return; sessions here aren't
    # supervised, so the tail is a no-op once this returns False.
    def is_session_cancelled(self, _app_session_id: str) -> bool:
        return False


def test_scoping_user_turns_enable_supervisor_does_not(
    client: TestClient,
) -> bool:
    from orchs.native import handle_turn as native_handle_turn
    from orchs.supervisor import run_primary_turn

    sid = _new_session(mode="native")
    sess = session_store.get_session(sid)
    sid_m = _new_session(mode="manager")
    sess_m = session_store.get_session(sid_m)

    async def acb(_ev) -> None:
        return None

    async def run() -> tuple[object, object, object]:
        fc_n = _FakeCoord()
        await native_handle_turn(
            fc_n, session=sess, prompt="p", app_session_id=sid,
            model="m", cwd="/tmp", ws_callback=acb, images=None,
            client_id="c",
        )
        fc_m = _FakeCoord()
        await native_handle_turn(
            fc_m, session=sess_m, prompt="p", app_session_id=sid_m,
            model="m", cwd="/tmp", ws_callback=acb, images=None,
        )
        fc_s = _FakeCoord()
        await run_primary_turn(
            fc_s, app_session_id=sid, prompt="p", ws_callback=acb,
        )
        return fc_n, fc_m, fc_s

    fc_n, fc_m, fc_s = asyncio.run(run())

    if not fc_n.calls or fc_n.calls[-1].get("user_initiated") is not True:
        print(f"  native handle_turn must set user_initiated=True: {fc_n.calls}")
        return False
    if not fc_m.calls or fc_m.calls[-1].get("user_initiated") is not True:
        print(f"  manager handle_turn must set user_initiated=True: {fc_m.calls}")
        return False
    if not fc_s.calls:
        print("  run_primary_turn did not invoke run_turn")
        return False
    if fc_s.calls[-1].get("user_initiated"):
        print(
            "  SCOPING LEAK: supervisor run_primary_turn set "
            f"user_initiated truthy: {fc_s.calls[-1].get('user_initiated')}"
        )
        return False
    return True


def test_run_primary_kwargs_per_site(client: TestClient) -> bool:
    """Lock FIX A: handle_turn (native + manager mode) + supervisor
    run_primary_turn all funnel through the single
    OrchestrationStrategy.run_primary, which forwards the flat
    session_id_field='agent_session_id' / mode='native' /
    trace_step_name='native_turn'. The ONLY per-mode difference is
    cli_prompt: native-mode session = identity, manager-mode session =
    BOOTSTRAP-wrapped (driven by session['orchestration_mode'] inside
    wrap_cli_prompt)."""
    from orchs.native import handle_turn as native_handle_turn
    from orchs.supervisor import run_primary_turn

    sid_n = _new_session(mode="native")
    sess_n = session_store.get_session(sid_n)
    sid_m = _new_session(mode="manager")
    sess_m = session_store.get_session(sid_m)

    async def acb(_ev) -> None:
        return None

    async def run():
        fc_n = _FakeCoord()
        await native_handle_turn(
            fc_n, session=sess_n, prompt="p", app_session_id=sid_n,
            model="m", cwd="/tmp", ws_callback=acb, images=None,
            client_id="c",
        )
        fc_m = _FakeCoord()
        await native_handle_turn(
            fc_m, session=sess_m, prompt="p", app_session_id=sid_m,
            model="m", cwd="/tmp", ws_callback=acb, images=None,
        )
        fc_s = _FakeCoord()
        await run_primary_turn(
            fc_s, app_session_id=sid_n, prompt="p", ws_callback=acb,
            source="supervisor",
        )
        return fc_n, fc_m, fc_s

    fc_n, fc_m, fc_s = asyncio.run(run())

    n = fc_n.calls[-1]
    if (n.get("session_id_field") != "agent_session_id"
            or n.get("mode") != "native"
            or n.get("trace_step_name") != "native_turn"):
        print(f"  native run_primary kwargs wrong: {n}")
        return False
    if n.get("cli_prompt") != "p":
        print(f"  native cli_prompt should be identity 'p': {n.get('cli_prompt')!r}")
        return False
    if n.get("client_id") != "c":
        print(f"  native client_id not forwarded: {n}")
        return False

    m = fc_m.calls[-1]
    if (m.get("session_id_field") != "agent_session_id"
            or m.get("mode") != "team"
            or m.get("trace_step_name") != "native_turn"):
        print(f"  manager-mode run_primary kwargs wrong: {m}")
        return False
    cp = m.get("cli_prompt") or ""
    if cp == "p" or "p" not in cp:
        print(f"  manager-mode cli_prompt should be BOOTSTRAP-wrapped, got: {cp!r}")
        return False

    s = fc_s.calls[-1]
    if (s.get("session_id_field") != "agent_session_id"
            or s.get("mode") != "native"
            or s.get("source") != "supervisor"):
        print(f"  run_primary_turn (native session) kwargs wrong: {s}")
        return False
    return True


TESTS = [
    ("new session seeds open_file_panels=[]", test_new_session_seeds_empty_panels),
    ("legacy session migrates with []", test_legacy_session_migrates_with_empty_panels),
    ("POST persists + de-dupes by path", test_post_persists_and_dedupes_by_path),
    ("DELETE removes panel", test_delete_removes_panel),
    ("PUT replaces + reorders", test_put_replaces_and_reorders),
    ("POST missing path → 400", test_post_missing_path_is_400),
    ("POST missing session → 404", test_post_missing_session_is_404),
    ("file metadata changes after write", test_file_metadata_changes_after_write),
    ("POST emits session_metadata_updated", test_post_emits_ws_with_origin),
    ("DELETE emits session_metadata_updated", test_delete_emits_ws_with_origin_query),
    ("internal bad token → 403", test_internal_bad_token_403),
    ("internal mode=panel mutates state", test_internal_panel_mode_mutates),
    ("internal mode=inline does NOT mutate", test_internal_inline_mode_does_not_mutate),
    ("internal resolves relative path vs cwd", test_internal_relative_path_resolved),
    ("internal missing session → success False", test_internal_missing_session_returns_failure),
    ("internal bad mode → success False", test_internal_bad_mode_returns_failure),
    ("SCOPING: user turns enable, supervisor does not", test_scoping_user_turns_enable_supervisor_does_not),
    ("FIX A: run_primary forwards per-site kwargs", test_run_primary_kwargs_per_site),
]


def main_run() -> int:
    # `with` runs the app lifespan so session_manager binds the running
    # event loop — required for the WS-broadcast path (`_fire` schedules
    # the bus publish onto that loop).
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        authenticate_client(client)
        return _run_tests(client)


def _run_tests(client: TestClient) -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn(client)
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())

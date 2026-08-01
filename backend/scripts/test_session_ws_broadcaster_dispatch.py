"""Unit-tier coverage of the SessionWSBroadcaster change-kind → WS-frame
mappings in session_ws_broadcaster.py.

The broadcaster's whole job is mapping each typed `on_change` kind to the
correct WS frame (session_metadata_updated / messages_delta / typed event).
The sibling test files cover only a handful of kinds; this file exercises the
remaining mappings end-to-end: every kind is fired through a real
SessionWSBroadcaster with a CapturingCoordinator and the dispatched frame's
type + payload are asserted exactly.

Also covers `_dispatch`'s loop-resolution paths (running loop, bound loop,
no-loop drop) and the unknown-kind first-occurrence warning + dedup.

Isolated BETTER_AGENT_HOME via _test_home.isolate_installed (the metadata
arms that reach into session_manager need an active installation profile for
session creation; pure on_change kinds do not, but the shared home keeps the
file self-contained and consistent with its siblings).
"""

from __future__ import annotations

import asyncio
import os
import sys

import _test_home

_test_home.isolate_installed("bc-test-ws-dispatch-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_ws_broadcaster  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402
from _ws_bcast_support import CapturingCoordinator, drive  # noqa: E402


def _bcast() -> tuple[SessionWSBroadcaster, list[dict]]:
    captured: list[dict] = []
    return SessionWSBroadcaster(CapturingCoordinator(captured)), captured


def _meta(sid: str, patch: dict, client_id=None) -> dict:
    return {
        "type": "session_metadata_updated",
        "data": {
            "session_id": sid,
            "patch": patch,
            "originated_by": client_id,
        },
    }


def _fire(kind: str, sid: str = "s1", **change) -> list[dict]:
    """Drive one change of the given kind and return the captured frames."""
    b, captured = _bcast()
    drive(b, sid, {"kind": kind, **change})
    return captured


# --------------------------------------------------------------------------- #
# project-keyed pings (carry cwd + node_id from _project_key_for)
# --------------------------------------------------------------------------- #


class TestProjectKeyed:
    # A fake sid is not in session_manager, so _project_key_for returns ("", "primary").

    def test_provenance_changed(self):
        assert _fire("provenance_changed") == [
            {"type": "session_provenance_changed", "data": {"session_id": "s1"}}
        ]

    def test_monitoring_changed(self):
        assert _fire("monitoring_changed", value="blocked_on_user") == [{
            "type": "session_monitoring_changed",
            "data": {
                "session_id": "s1",
                "monitoring_state": "blocked_on_user",
                "cwd": "",
                "node_id": "primary",
            },
        }]

    def test_error_changed(self):
        assert _fire("error_changed", has_error=True) == [{
            "type": "session_error_changed",
            "data": {
                "session_id": "s1",
                "has_error": True,
                "cwd": "",
                "node_id": "primary",
            },
        }]

    def test_marker_set(self):
        assert _fire("marker_set", extension_id="ext-1", marker="dot") == [{
            "type": "session_marker_changed",
            "data": {
                "session_id": "s1",
                "extension_id": "ext-1",
                "marker": "dot",
                "cwd": "",
                "node_id": "primary",
            },
        }]

    def test_marker_cleared(self):
        assert _fire("marker_cleared", extension_id="ext-1") == [{
            "type": "session_marker_changed",
            "data": {
                "session_id": "s1",
                "extension_id": "ext-1",
                "marker": None,
                "cwd": "",
                "node_id": "primary",
            },
        }]

    def test_project_key_falls_back_on_lookup_error(self):
        # If manager.get_project_key raises, _project_key_for returns
        # ("", "primary") and the frame is still emitted — the broadcaster must
        # never crash the watcher thread that fires on_change.
        import session_manager

        mgr = session_manager.manager
        orig = mgr.get_project_key

        def _raise(_sid):
            raise RuntimeError("lookup failed")

        mgr.get_project_key = _raise
        try:
            captured = _fire("running_changed", value=True)
        finally:
            mgr.get_project_key = orig
        assert captured == [{
            "type": "session_running_changed",
            "data": {"session_id": "s1", "value": True, "cwd": "", "node_id": "primary"},
        }]


# --------------------------------------------------------------------------- #
# message-scoped typed events
# --------------------------------------------------------------------------- #


class TestMessageEvents:
    def test_msg_recovering_set(self):
        assert _fire("msg_recovering_set", msg_id="m1", value=True) == [{
            "type": "message_recovering_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "value": True},
        }]

    def test_completed_at_set_emits_delta(self):
        msg = {"id": "m1", "content": "done"}
        captured = _fire("completed_at_set", msg=msg)
        assert captured[0]["type"] == "messages_delta"
        assert captured[0]["data"] == {"app_session_id": "s1", "messages": [msg]}

    def test_completed_at_set_no_msg_is_noop(self):
        assert _fire("completed_at_set") == []

    def test_running_content_updated(self):
        assert _fire("running_content_updated", msg_id="m1", content="hi") == [{
            "type": "message_content_updated",
            "data": {"session_id": "s1", "msg_id": "m1", "content": "hi"},
        }]

    def test_message_content_materialized(self):
        assert _fire("message_content_materialized", msg_id="m1", content="x") == [{
            "type": "message_content_updated",
            "data": {"session_id": "s1", "msg_id": "m1", "content": "x"},
        }]

    def test_user_msg_marked_error_emits_delta(self):
        msg = {"id": "m1", "content": "prompt"}
        captured = _fire("user_msg_marked_error", msg=msg)
        assert captured[0]["type"] == "messages_delta"
        assert captured[0]["data"] == {"app_session_id": "s1", "messages": [msg]}

    def test_user_msg_marked_error_no_msg_is_noop(self):
        assert _fire("user_msg_marked_error") == []

    def test_user_msg_appended_emits_delta(self):
        msg = {"id": "m1", "content": "u"}
        captured = _fire("user_msg_appended", msg=msg)
        assert captured[0]["type"] == "messages_delta"
        assert captured[0]["data"]["messages"] == [msg]

    def test_assistant_msg_appended_emits_delta(self):
        msg = {"id": "m1", "content": "a"}
        captured = _fire("assistant_msg_appended", msg=msg)
        assert captured[0]["type"] == "messages_delta"
        assert captured[0]["data"]["messages"] == [msg]

    def test_assistant_error_set(self):
        assert _fire("assistant_error_set", msg_id="m1", text="boom") == [{
            "type": "message_error_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "error_text": "boom"},
        }]

    def test_msg_retrying_set(self):
        assert _fire("msg_retrying_set", msg_id="m1", retry_at="t") == [{
            "type": "message_retrying_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "retry_at": "t"},
        }]

    def test_msg_auto_retry_set(self):
        assert _fire("msg_auto_retry_set", msg_id="m1", auto_retry=2) == [{
            "type": "message_auto_retry_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "auto_retry": 2},
        }]

    def test_msg_continuation_set(self):
        assert _fire("msg_continuation_set", msg_id="m1", chain_depth=3) == [{
            "type": "message_continuation_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "chain_depth": 3},
        }]

    def test_msg_run_meta_set(self):
        meta = {"provider_id": "p", "model": "m"}
        assert _fire("msg_run_meta_set", msg_id="m1", run_meta=meta) == [{
            "type": "message_run_meta_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "run_meta": meta},
        }]

    def test_msg_ask_result_set(self):
        assert _fire("msg_ask_result_set", msg_id="m1", ask_result={"x": 1}) == [{
            "type": "message_ask_result_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "ask_result": {"x": 1}},
        }]

    def test_msg_ask_choice_set(self):
        assert _fire("msg_ask_choice_set", msg_id="m1", chosen_session_id="c1") == [{
            "type": "message_ask_choice_changed",
            "data": {"session_id": "s1", "msg_id": "m1", "chosen_session_id": "c1"},
        }]

    def test_journal_event_projected_with_explicit_delta(self):
        delta = {"id": "m1", "content": "c"}
        captured = _fire("journal_event_projected", delta=delta)
        assert captured == [{
            "type": "messages_delta",
            "data": {"app_session_id": "s1", "messages": [delta]},
        }]

    def test_journal_event_projected_no_delta_no_msg_is_noop(self):
        # delta absent and no msg to compact from → nothing to dispatch.
        assert _fire("journal_event_projected") == []

    def test_assistant_msg_appended_no_msg_is_noop(self):
        assert _fire("assistant_msg_appended") == []


# --------------------------------------------------------------------------- #
# session lifecycle (multi-tab convergence)
# --------------------------------------------------------------------------- #


class TestLifecycle:
    def test_forked(self):
        sess = {"id": "fork-1"}
        assert _fire("forked", session=sess, parent_session_id="p1") == [{
            "type": "session_forked",
            "data": {"session": sess, "parent_session_id": "p1"},
        }]

    def test_created_emits_for_visible_session(self):
        sess = {"id": "new-1", "name": "n"}
        assert _fire("created", session=sess) == [{
            "type": "session_created",
            "data": {"session": sess},
        }]

    def test_created_filters_working_mode_session(self):
        # Sessions with a working_mode are ephemeral workers — never leaked to
        # the sidebar convergence frame.
        sess = {"id": "new-1", "working_mode": {"role": "file-edit"}}
        assert _fire("created", session=sess) == []

    def test_deleted(self):
        assert _fire("deleted", sid="d1") == [{
            "type": "session_deleted",
            "data": {"session_id": "d1"},
        }]

    def test_renamed(self):
        assert _fire("renamed", name="new name") == [{
            "type": "session_renamed",
            "data": {"session_id": "s1", "name": "new name"},
        }]


# --------------------------------------------------------------------------- #
# selectors_set patch building
# --------------------------------------------------------------------------- #


class TestSelectorsSet:
    def test_single_field_patch(self):
        assert _fire("selectors_set", model="sonnet") == [
            _meta("s1", {"model": "sonnet", "selectors_seq": 0})
        ]

    def test_all_fields_patch(self):
        captured = _fire(
            "selectors_set",
            model="m",
            reasoning_effort="high",
            cwd="/x",
            provider_id="p",
            client_id="tab-A",
        )
        assert captured == [_meta(
            "s1",
            {
                "model": "m", "reasoning_effort": "high", "cwd": "/x",
                "provider_id": "p", "selectors_seq": 0,
            },
            client_id="tab-A",
        )]

    def test_empty_change_is_noop(self):
        assert _fire("selectors_set") == []

    def test_selectors_seq_propagates_from_change_dict(self):
        # `selectors_seq` lets multi-tab clients tell "the session's
        # selector state advanced since I last looked" (adopt) apart from
        # "my own local pick hasn't round-tripped yet" (persist) — see
        # `SessionManager._bump_selectors_seq` / `resolveModelDriftAction`.
        # The broadcaster must forward whatever seq SessionManager fired,
        # not just default to 0.
        assert _fire("selectors_set", model="opus", selectors_seq=7) == [
            _meta("s1", {"model": "opus", "selectors_seq": 7})
        ]


# --------------------------------------------------------------------------- #
# unknown-kind + internal-kind dispatch gating
# --------------------------------------------------------------------------- #


class TestKindGating:
    def test_internal_kind_silent_drop(self):
        captured, seen = _capture_with_warning("agent_sid_set")
        assert captured == []
        assert seen == []  # internal kinds never warn

    def test_unknown_kind_warns_once_then_dedups(self):
        novel = "totally_novel_kind_xyz_unique"
        captured, seen = _capture_with_warning(novel)
        assert captured == []  # unknown kinds dispatch no frame
        assert len(seen) == 1  # first occurrence warns
        assert novel in seen[0][1]

        # Second fire of the same kind is suppressed by the per-process dedup.
        captured2, seen2 = _capture_with_warning(novel)
        assert captured2 == []
        assert seen2 == []


def _capture_with_warning(kind: str) -> tuple[list[dict], list]:
    """Drive one change of `kind` while capturing logger.warning calls."""
    captured: list[dict] = []
    seen: list = []
    original = session_ws_broadcaster.logger.warning
    session_ws_broadcaster.logger.warning = lambda *a, **k: seen.append(a)
    try:
        b = SessionWSBroadcaster(CapturingCoordinator(captured))
        drive(b, "s1", {"kind": kind})
    finally:
        session_ws_broadcaster.logger.warning = original
    return captured, seen


# --------------------------------------------------------------------------- #
# metadata-kind patches (session_metadata_updated)
# --------------------------------------------------------------------------- #


class TestMetadataPatches:
    def test_draft_set(self):
        assert _fire("draft_set", text="hello", seq=5) == [
            _meta("s1", {"draft_input": "hello", "draft_input_seq": 5})
        ]

    def test_draft_set_with_images(self):
        assert _fire("draft_set", text="hi", seq=1, images=["a.png"]) == [
            _meta("s1", {"draft_input": "hi", "draft_input_seq": 1, "draft_images": ["a.png"]})
        ]

    def test_fork_closed_set(self):
        assert _fire("fork_closed_set", value=True) == [_meta("s1", {"fork_closed": True})]

    def test_supervisor_enabled_set(self):
        assert _fire("supervisor_enabled_set", value=True) == [
            _meta("s1", {"supervisor_enabled": True})
        ]

    def test_supervisor_enabled_set_with_custom_prompt(self):
        assert _fire(
            "supervisor_enabled_set", value=True, supervisor_custom_prompt="be nice"
        ) == [_meta("s1", {"supervisor_enabled": True, "supervisor_custom_prompt": "be nice"})]

    def test_supervisor_separated(self):
        assert _fire("supervisor_separated", old_supervisor_sid="sup-1") == [_meta("s1", {
            "supervisor_agent_session_id": None,
            "forked_from_supervisor_agent_sid": "sup-1",
            "supervisor_bootstrap_received": False,
        })]

    def test_pinned_set(self):
        assert _fire("pinned_set", value=True) == [_meta("s1", {"pinned": True})]

    def test_topbar_pinned_set(self):
        assert _fire("topbar_pinned_set", value=True, topbar_pinned_at="t") == [
            _meta("s1", {"topbar_pinned": True, "topbar_pinned_at": "t"})
        ]

    def test_archived_set(self):
        assert _fire("archived_set", value=True) == [_meta("s1", {"archived": True})]

    def test_all_projects_set(self):
        assert _fire("all_projects_set", all_projects=True) == [
            _meta("s1", {"all_projects": True})
        ]

    def test_worker_eligible_set(self):
        assert _fire("worker_eligible_set", value=True) == [_meta("s1", {"worker_eligible": True})]

    def test_worker_creation_policy_set(self):
        assert _fire("worker_creation_policy_set", policy="auto") == [
            _meta("s1", {"worker_creation_policy": "auto"})
        ]

    def test_worker_creation_policy_set_defaults_to_ask(self):
        assert _fire("worker_creation_policy_set", policy="") == [
            _meta("s1", {"worker_creation_policy": "ask"})
        ]

    def test_capability_contexts_set(self):
        ctx = [{"capability_id": "x:c"}]
        assert _fire("capability_contexts_set", capability_contexts=ctx) == [
            _meta("s1", {"capability_contexts": ctx})
        ]

    def test_open_panels_set(self):
        panels = [{"id": "p1"}]
        assert _fire("open_panels_set", open_file_panels=panels) == [
            _meta("s1", {"open_file_panels": panels})
        ]

    def test_open_config_panels_set(self):
        panels = [{"id": "c1"}]
        assert _fire("open_config_panels_set", open_config_panels=panels) == [
            _meta("s1", {"open_config_panels": panels})
        ]

    def test_working_mode_marked(self):
        wm = {"role": "file-edit"}
        meta = {"files": ["a.ts"]}
        assert _fire("working_mode_marked", working_mode=wm, working_mode_meta=meta) == [
            _meta("s1", {"working_mode": wm, "working_mode_meta": meta})
        ]

    def test_notes_updated(self):
        notes = [{"body": "n"}]
        assert _fire("notes_updated", notes=notes) == [_meta("s1", {"notes": notes})]

    def test_todos_updated(self):
        todos = [{"subject": "s"}]
        assert _fire("todos_updated", current_todos=todos) == [_meta("s1", {"current_todos": todos})]

    def test_todos_snapshot_dispatches_standalone(self):
        # todos_snapshot is NOT a metadata patch — it has its own frame type
        # so the frontend routes it onto the streaming message, not metadata.
        todos = [{"subject": "s"}]
        assert _fire("todos_snapshot", todos=todos) == [{
            "type": "todos_snapshot",
            "data": {"app_session_id": "s1", "session_id": "s1", "todos": todos},
        }]

    def test_tasks_updated(self):
        tasks = [{"subject": "t"}]
        assert _fire("tasks_updated", current_tasks=tasks) == [_meta("s1", {"current_tasks": tasks})]

    def test_right_panel_set_open_only(self):
        assert _fire("right_panel_set", right_panel_open=True) == [
            _meta("s1", {"right_panel_open": True})
        ]

    def test_right_panel_set_tab_without_open(self):
        # right_panel_open absent → it is omitted from the patch, but other
        # mutated keys still flow through.
        assert _fire("right_panel_set", right_panel_active_tab="todos") == [
            _meta("s1", {"right_panel_active_tab": "todos"})
        ]

    def test_right_panel_set_all_keys(self):
        captured = _fire(
            "right_panel_set",
            right_panel_open=True,
            right_panel_active_tab="todos",
            right_panel_width=400,
            right_panel_mobile_height=300,
            right_panel_todos_dismissed=True,
            right_panel_auto_opened_by=["task"],
            sidebar_minimized=False,
        )
        assert captured == [_meta("s1", {
            "right_panel_open": True,
            "right_panel_active_tab": "todos",
            "right_panel_width": 400,
            "right_panel_mobile_height": 300,
            "right_panel_todos_dismissed": True,
            "right_panel_auto_opened_by": ["task"],
            "sidebar_minimized": False,
        })]

    def test_queued_prompts_updated(self):
        q = [{"id": "q1"}]
        assert _fire("queued_prompts_updated", queued_prompts=q, queued_prompts_seq=7) == [
            _meta("s1", {"queued_prompts": q, "queued_prompts_seq": 7})
        ]

    def test_queued_prompts_updated_defaults_seq_zero(self):
        q = [{"id": "q1"}]
        assert _fire("queued_prompts_updated", queued_prompts=q) == [
            _meta("s1", {"queued_prompts": q, "queued_prompts_seq": 0})
        ]

    def test_fork_provisioning_changed(self):
        fp = {"status": "pending"}
        assert _fire("fork_provisioning_changed", fork_provisioning=fp) == [
            _meta("s1", {"fork_provisioning": fp})
        ]

    def test_fork_provisioning_changed_null_on_clear(self):
        # Explicit null distinguishes "no longer pending" from "not present".
        assert _fire("fork_provisioning_changed", fork_provisioning=None) == [
            _meta("s1", {"fork_provisioning": None})
        ]

    def test_last_opened_set(self):
        assert _fire("last_opened_set", at="2026-01-01T00:00:00Z") == [
            _meta("s1", {"last_opened_at": "2026-01-01T00:00:00Z"})
        ]

    def test_tag_change_falls_through_to_inline_tags(self):
        tags = [{"id": "t1"}]
        assert _fire("tag_added", inline_tags=tags) == [_meta("s1", {"inline_tags": tags})]


# --------------------------------------------------------------------------- #
# _dispatch loop-resolution paths
# --------------------------------------------------------------------------- #


class TestDispatchLoopResolution:
    def test_no_loop_drops_frame(self):
        # Without a running or bound loop, _dispatch silently drops the frame
        # rather than raising — firing from a non-async context with no loop.
        captured: list[dict] = []
        b = SessionWSBroadcaster(CapturingCoordinator(captured))
        b.on_change("s1", {"kind": "deleted"})  # no drive(), no loop
        assert captured == []

    def test_running_loop_path_dispatches(self):
        # When on_change fires inside an actual running loop, _dispatch uses
        # asyncio.get_running_loop() and dispatches immediately.
        captured: list[dict] = []

        async def _fire():
            b = SessionWSBroadcaster(CapturingCoordinator(captured))
            b.on_change("s1", {"kind": "deleted"})

        asyncio.run(_fire())
        assert captured == [{
            "type": "session_deleted",
            "data": {"session_id": "s1"},
        }]

    def test_bound_loop_swallows_schedule_exception(self):
        # If schedule_global raises on the bound-loop path, _dispatch logs and
        # drops rather than propagating — the broadcaster must never crash the
        # watcher thread that fires on_change.

        class _RaisingCoord:
            def schedule_global(self, *_a, **_k):
                raise RuntimeError("boom")

        seen: list = []
        original = session_ws_broadcaster.logger.exception
        session_ws_broadcaster.logger.exception = lambda *a, **k: seen.append(a)
        try:
            b = SessionWSBroadcaster(_RaisingCoord())
            drive(b, "s1", {"kind": "deleted"})  # bound loop → schedule_global raises
        finally:
            session_ws_broadcaster.logger.exception = original
        assert seen  # logged + swallowed, did not raise out of drive()

    def test_originated_by_carried_from_client_id(self):
        # A metadata kind carries client_id through as originated_by so the
        # originating tab can filter its own echo.
        assert _fire("pinned_set", value=True, client_id="tab-Z") == [
            _meta("s1", {"pinned": True}, client_id="tab-Z")
        ]

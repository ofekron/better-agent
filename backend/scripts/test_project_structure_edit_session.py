#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import json
import shutil
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home
TMP_HOME = Path(_test_home.isolate("bc-project-structure-edit-"))

import _extension_test_helpers  # noqa: E402
import _fake_runtime  # noqa: E402
import config_store  # noqa: E402
import project_structure_edit_session  # noqa: E402
import project_update_store  # noqa: E402
import session_store  # noqa: E402
import working_mode  # noqa: E402
import virtual_session_store  # noqa: E402
from paths import encode_cwd  # noqa: E402
from provisioning.config import ProvisionedConfig  # noqa: E402
from provisioning.dispatch import dispatch  # noqa: E402
from provisioning.lifecycle import dirty_reason, ensure_session  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

_extension_test_helpers.install_extension_fixture(
    str(TMP_HOME),
    "test.project-structure",
    core_roles=["project-structure"],
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def reset_state() -> None:
    sessions_dir = TMP_HOME / "sessions"
    updates_dir = TMP_HOME / "project_updates"
    edit_dir = TMP_HOME / "project-structure-edit"
    for path in (sessions_dir, updates_dir, edit_dir):
        shutil.rmtree(path, ignore_errors=True)
    virtual_sessions = TMP_HOME / "virtual_sessions.json"
    virtual_sessions.unlink(missing_ok=True)
    with project_update_store._lock:
        project_update_store._counts_loaded = False
        project_update_store._unseen_counts.clear()
        project_update_store._total_unseen_count = 0
    session_store._summary_index.clear()
    session_store._fork_index.clear()
    session_store._summary_index_loaded = False
    session_store._index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()
    project_structure_edit_session._inflight = None
    project_structure_edit_session._inflight_queued_id = None


def visible_session() -> dict:
    return virtual_session_store.get(project_structure_edit_session.edit_singleton_id()) or {}


def append_visible_message(message: dict) -> None:
    session = visible_session()
    messages = list(session.get("messages") or [])
    messages.append(message)
    virtual_session_store.replace_messages(
        project_structure_edit_session.edit_extension_id(),
        project_structure_edit_session.edit_singleton_id(),
        messages,
    )


def make_project(skill_root: str = ".agents") -> Path:
    project = TMP_HOME / "repo"
    skill_dir = project / skill_root / "skills" / "project-structure"
    sections = skill_dir / "sections"
    sections.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# project structure\n", encoding="utf-8")
    (sections / "components.md").write_text("# components\n", encoding="utf-8")
    return project


def write_fake_jsonl(tmp_path: Path, user_turns: int, assistant_turns: int) -> Path:
    rows = []
    for i in range(user_turns):
        rows.append({"type": "user", "message": {"content": f"user-{i}"}})
    for i in range(assistant_turns):
        rows.append({"type": "assistant", "message": {"content": f"assistant-{i}"}})
    path = tmp_path / f"session-{user_turns}-{assistant_turns}.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


async def test_submit_review_prompt_enqueues_full_params() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    project_update_store.append(project_id, "New backend component exists.")
    enqueued: list[tuple[str, str, str]] = []
    original_enqueue = project_structure_edit_session._enqueue_review
    original_resolve_internal_llm = config_store.resolve_internal_llm
    project_structure_edit_session._enqueue_review = (
        lambda prompt, project_cwd, skill_dir, instructions=None, **kwargs: (
            enqueued.append((prompt, project_cwd, skill_dir)) or "queued-review"
        )
    )
    config_store.resolve_internal_llm = lambda _task: {
        "model": "claude-test",
        "provider_id": None,
        "reasoning_effort": "medium",
    }

    try:
        result = await project_structure_edit_session.submit_review_prompt(str(project))
    finally:
        project_structure_edit_session._enqueue_review = original_enqueue
        config_store.resolve_internal_llm = original_resolve_internal_llm

    check(result["status"] == "ok", "submit_review_prompt returns ok")
    check(result["queued_id"] == "queued-review", "submit_review_prompt returns queued id")
    check(len(enqueued) == 1, "submit_review_prompt enqueues exactly once")
    prompt, enqueued_project, skill_dir = enqueued[0]
    check(enqueued_project == str(project), "enqueue uses project cwd")
    check(skill_dir.endswith(".agents/skills/project-structure"), "enqueue uses skill dir")
    check("Requirement guard" in prompt, "prompt includes requirement guard")
    check(
        "must never contradict user requirements" in prompt,
        "prompt forbids contradicting user requirements",
    )
    check("get-requirements" in prompt, "prompt requires get-requirements check")
    check("minimize user intervention" in prompt, "prompt minimizes user intervention")
    check("last resort" in prompt, "prompt asks user only as last resort")


async def test_submit_review_prompt_no_unseen_does_not_submit() -> None:
    reset_state()
    project = make_project()
    enqueued: list[tuple[str, str, str]] = []
    original_enqueue = project_structure_edit_session._enqueue_review
    project_structure_edit_session._enqueue_review = (
        lambda prompt, project_cwd, skill_dir, instructions=None, **kwargs: (
            enqueued.append((prompt, project_cwd, skill_dir)) or "queued-review"
        )
    )

    try:
        result = await project_structure_edit_session.submit_review_prompt(str(project))
    finally:
        project_structure_edit_session._enqueue_review = original_enqueue

    check(result["error"] == "no_unseen_updates", "no unseen updates returns no_unseen_updates")
    check(enqueued == [], "no unseen updates does not enqueue")


async def test_submit_review_prompt_reuses_active_review() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    project_update_store.append(project_id, "New backend component exists.")
    original_resolve_internal_llm = config_store.resolve_internal_llm
    config_store.resolve_internal_llm = lambda _task: {
        "model": "claude-test",
        "provider_id": None,
        "reasoning_effort": "medium",
    }
    task = asyncio.create_task(asyncio.sleep(60))
    project_structure_edit_session._inflight = task
    project_structure_edit_session._inflight_queued_id = "active-review"

    try:
        result = await project_structure_edit_session.submit_review_prompt(str(project))
    finally:
        task.cancel()
        project_structure_edit_session._inflight = None
        project_structure_edit_session._inflight_queued_id = None
        config_store.resolve_internal_llm = original_resolve_internal_llm
        try:
            await task
        except asyncio.CancelledError:
            pass

    check(result["status"] == "already_running", "active review returns already_running")
    check(result["queued_id"] == "active-review", "active review returns existing queued id")


async def test_concurrent_submit_review_prompt_enqueues_once() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    project_update_store.append(project_id, "New backend component exists.")
    await project_structure_edit_session.ensure_singleton(str(project))
    started = asyncio.Event()
    release = asyncio.Event()
    original_run_review = project_structure_edit_session._run_review

    async def fake_run_review(*args, **kwargs):
        started.set()
        await release.wait()

    project_structure_edit_session._run_review = fake_run_review

    try:
        first, second = await asyncio.gather(
            project_structure_edit_session.submit_review_prompt(str(project)),
            project_structure_edit_session.submit_review_prompt(str(project)),
        )
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        release.set()
        task = project_structure_edit_session._inflight
        if task is not None:
            await task
        project_structure_edit_session._run_review = original_run_review
        project_structure_edit_session._inflight = None
        project_structure_edit_session._inflight_queued_id = None

    statuses = sorted([first["status"], second["status"]])
    queued_ids = {first["queued_id"], second["queued_id"]}
    check(statuses == ["already_running", "ok"], "concurrent review submits queue once")
    check(len(queued_ids) == 1, "concurrent review submits share queued id")


async def test_find_skill_dir_prefers_agents_over_claude() -> None:
    reset_state()
    project = make_project()
    claude_skill_dir = project / ".claude" / "skills" / "project-structure"
    (claude_skill_dir / "sections").mkdir(parents=True, exist_ok=True)
    (claude_skill_dir / "SKILL.md").write_text("# legacy\n", encoding="utf-8")

    skill_dir = project_structure_edit_session._find_skill_dir(str(project))

    check(
        skill_dir is not None and skill_dir.endswith(".agents/skills/project-structure"),
        "skill discovery prefers .agents over .claude",
    )


def test_maintainer_dirty_policy_accepts_valid_provision_shape() -> None:
    reset_state()
    project = make_project()
    clean_path = write_fake_jsonl(project, 1, 2)
    many_assistant_path = write_fake_jsonl(project, 1, 20)
    leaked_query_path = write_fake_jsonl(project, 2, 1)
    import orchs.jsonl_helpers as jsonl_helpers

    original = jsonl_helpers.compute_jsonl_path
    try:
        jsonl_helpers.compute_jsonl_path = lambda cwd, sid: (
            clean_path
            if sid == "clean-sid"
            else many_assistant_path
            if sid == "many-assistant-sid"
            else leaked_query_path
        )
        clean = {"agent_session_id": "clean-sid"}
        many_assistant = {"agent_session_id": "many-assistant-sid"}
        leaked_query = {"agent_session_id": "leaked-query-sid"}
        check(
            dirty_reason(
                clean,
                project_structure_edit_session.MAINTAINER_SPEC.dirty_policy,
                str(project),
            ) == "",
            "maintainer dirty policy accepts one user and two assistant rows",
        )
        check(
            dirty_reason(
                many_assistant,
                project_structure_edit_session.MAINTAINER_SPEC.dirty_policy,
                str(project),
            ) == "",
            "maintainer dirty policy allows many assistant provision rows",
        )
        check(
            "user turns" in dirty_reason(
                leaked_query,
                project_structure_edit_session.MAINTAINER_SPEC.dirty_policy,
                str(project),
            ),
            "maintainer dirty policy still rejects extra user turns",
        )
    finally:
        jsonl_helpers.compute_jsonl_path = original


def test_maintainer_ensure_session_reuses_valid_provisioned_base() -> None:
    reset_state()
    project = make_project()
    clean_path = write_fake_jsonl(project, 1, 2)
    sess = session_manager.create(
        name="project-structure-maintainer",
        orchestration_mode="native",
        cwd=str(project),
        model="claude-test",
        source="internal",
        provider_id="provider-test",
        worker_creation_policy="deny",
        bare_config=False,
    )
    working_mode.mark_working_mode(
        sess["id"],
        mode=project_structure_edit_session.MAINTAINER_WORKER_MODE,
        meta={
            "cwd": str(project),
            "provider_id": "provider-test",
            "model": "claude-test",
            "machine_completion": False,
            "version": project_structure_edit_session.MAINTAINER_SPEC.version,
            "node_id": "primary",
        },
    )
    session_manager.set_agent_sid(sess["id"], "native", "clean-sid")
    session_manager.flush_pending_persists()
    cfg = ProvisionedConfig(
        cwd=str(project),
        model="claude-test",
        provider_id="provider-test",
        reasoning_effort="",
        run_mode="fork",
        dispatch="in_process",
        on_no_fork="error",
        node_id="primary",
        backend_url="http://localhost:8000",
        internal_token="",
        provisioned_session_id=None,
        caller_session_id=project_structure_edit_session.edit_singleton_id(),
        worker_description="project-structure-maintainer",
    )
    import orchs.jsonl_helpers as jsonl_helpers

    original = jsonl_helpers.compute_jsonl_path
    try:
        jsonl_helpers.compute_jsonl_path = lambda cwd, sid: clean_path
        reused_id = ensure_session(project_structure_edit_session.MAINTAINER_SPEC, cfg)
    finally:
        jsonl_helpers.compute_jsonl_path = original

    check(reused_id == sess["id"], "ensure_session reuses valid maintainer base")


async def test_prepare_review_prompt_does_not_enqueue() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    project_update_store.append(project_id, "New backend component exists.")
    enqueued: list[str] = []
    original_enqueue = project_structure_edit_session._enqueue_review
    original_resolve_internal_llm = config_store.resolve_internal_llm
    project_structure_edit_session._enqueue_review = (
        lambda prompt, project_cwd, skill_dir, instructions=None, **kwargs: (
            enqueued.append(prompt) or "queued-review"
        )
    )
    config_store.resolve_internal_llm = lambda _task: {
        "model": "claude-test",
        "provider_id": None,
        "reasoning_effort": "medium",
    }

    try:
        result = await project_structure_edit_session.prepare_review_prompt(str(project))
    finally:
        project_structure_edit_session._enqueue_review = original_enqueue
        config_store.resolve_internal_llm = original_resolve_internal_llm

    check(result["status"] == "ok", "prepare_review_prompt returns ok")
    check(
        isinstance(result["review_prompt"], str) and result["review_prompt"],
        "prepare_review_prompt returns prompt",
    )
    check(enqueued == [], "prepare_review_prompt does not enqueue")
    check(
        visible_session().get("virtual") is True,
        "prepare_review_prompt ensures virtual edit singleton",
    )


async def test_prepare_review_prompt_supports_agents_skill_dir() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    project_update_store.append(project_id, "New backend component exists.")
    original_resolve_internal_llm = config_store.resolve_internal_llm
    config_store.resolve_internal_llm = lambda _task: {
        "model": "claude-test",
        "provider_id": None,
        "reasoning_effort": "medium",
    }

    try:
        result = await project_structure_edit_session.prepare_review_prompt(str(project))
    finally:
        config_store.resolve_internal_llm = original_resolve_internal_llm

    check(result["status"] == "ok", "prepare_review_prompt supports .agents skill dir")
    check(
        ".agents/skills/project-structure" in result["review_prompt"],
        "review prompt points at .agents skill dir",
    )


async def test_ensure_singleton_refreshes_project_cwd_meta() -> None:
    reset_state()
    first_project = make_project()
    second_project = TMP_HOME / "repo-two"
    skill_dir = second_project / ".agents" / "skills" / "project-structure"
    (skill_dir / "sections").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# project structure\n", encoding="utf-8")
    original_resolve_internal_llm = config_store.resolve_internal_llm
    config_store.resolve_internal_llm = lambda _task: {
        "model": "claude-test",
        "provider_id": None,
        "reasoning_effort": "medium",
    }

    try:
        await project_structure_edit_session.ensure_singleton(str(first_project))
        await project_structure_edit_session.ensure_singleton(str(second_project))
    finally:
        config_store.resolve_internal_llm = original_resolve_internal_llm

    check(
        project_structure_edit_session.get_singleton_project_cwd("fallback")
        == str(second_project.resolve()),
        "ensure_singleton refreshes real project cwd metadata",
    )
    check(
        visible_session().get("orchestration_mode") == "virtual",
        "ensure_singleton uses virtual visible edit session",
    )


async def test_submit_user_prompt_missing_skill_dir_fails_without_visible_turn() -> None:
    reset_state()
    project = TMP_HOME / "repo-without-skill"
    project.mkdir(parents=True, exist_ok=True)
    original_resolve_internal_llm = config_store.resolve_internal_llm
    config_store.resolve_internal_llm = lambda _task: {
        "model": "claude-test",
        "provider_id": None,
        "reasoning_effort": "medium",
    }

    try:
        result = await project_structure_edit_session.submit_user_prompt(
            str(project),
            "apply",
            client_id="missing-skill-client",
        )
    finally:
        config_store.resolve_internal_llm = original_resolve_internal_llm

    messages = visible_session().get("messages", [])
    check(result["error"] == "skill_dir_not_found", "missing skill dir returns explicit error")
    check(messages == [], "missing skill dir does not append stuck visible turn")


async def test_submit_user_prompt_routes_followup_through_provisioned_enqueue() -> None:
    reset_state()
    project = make_project()
    await project_structure_edit_session.ensure_singleton(str(project))
    append_visible_message({
        "id": "summary",
        "role": "assistant",
        "content": "Summary to apply.",
        "events": [],
        "timestamp": "now",
    })
    enqueued: list[tuple[str, str, str, str | None]] = []
    original_enqueue = project_structure_edit_session._enqueue_review
    project_structure_edit_session._enqueue_review = (
        lambda prompt, project_cwd, skill_dir, instructions=None, **kwargs: (
            enqueued.append((prompt, project_cwd, skill_dir, instructions)) or "queued-followup"
        )
    )

    try:
        result = await project_structure_edit_session.submit_user_prompt(str(project), "apply")
    finally:
        project_structure_edit_session._enqueue_review = original_enqueue

    check(result["status"] == "ok", "submit_user_prompt returns ok")
    check(result["queued_id"] == "queued-followup", "submit_user_prompt returns queued id")
    check(len(enqueued) == 1, "submit_user_prompt enqueues exactly once")
    prompt, project_cwd, skill_dir, instructions = enqueued[0]
    check(prompt == "apply", "followup visible prompt is raw user message")
    check(project_cwd == str(project), "followup uses project cwd")
    check(skill_dir.endswith(".agents/skills/project-structure"), "followup uses skill dir")
    check(isinstance(instructions, str), "followup passes worker instructions")
    check(
        "Summary to apply." not in instructions,
        "followup instructions do not inject visible transcript",
    )
    check(
        "existing maintainer fork conversation" in instructions,
        "followup instructions rely on persistent fork context",
    )
    check(
        "must never contradict user requirements" in instructions,
        "followup instructions include requirement guard",
    )
    check("get-requirements" in instructions, "followup instructions require requirements check")
    check(
        "Ask back only as a last" in instructions and "resort" in instructions,
        "followup asks back only as last resort",
    )


async def test_project_structure_visible_turn_acks_user_message() -> None:
    reset_state()
    project = make_project()
    await project_structure_edit_session.ensure_singleton(str(project))
    deltas: list[tuple[str, str]] = []
    acks: list[dict] = []

    class FakeCoordinator:
        async def _dispatch_messages_delta(self, root_id: str, sid: str, msg: dict, *, omit_render_events: bool = False):
            deltas.append((root_id, msg["role"]))

    _runtime_token = _fake_runtime.activate(FakeCoordinator())
    try:
        await project_structure_edit_session._append_visible_turn(
            "apply",
            "assistant-1",
            is_streaming=True,
            client_id="edit-client-1",
            lifecycle_msg_id="life-edit-1",
            on_user_message=lambda msg: _record_ack(acks, msg),
        )
    finally:
        _fake_runtime.deactivate(_runtime_token)

    messages = visible_session()["messages"]
    user_msg = messages[0]
    check(user_msg["client_id"] == "edit-client-1", "visible turn stores client id")
    check(
        user_msg["lifecycle_msg_id"] == "life-edit-1",
        "visible turn stores lifecycle id",
    )
    check(acks == [user_msg], "visible turn acks persisted user message")
    check(messages[1]["content"] == "", "visible turn defaults assistant content to empty")
    check(
        deltas == [
            (project_structure_edit_session.edit_singleton_id(), "user"),
            (project_structure_edit_session.edit_singleton_id(), "assistant"),
        ],
        "visible turn dispatches user then assistant deltas",
    )


async def test_project_structure_visible_turn_can_show_initial_assistant_content() -> None:
    reset_state()
    project = make_project()
    await project_structure_edit_session.ensure_singleton(str(project))

    class FakeCoordinator:
        async def _dispatch_messages_delta(self, root_id: str, sid: str, msg: dict, *, omit_render_events: bool = False):
            return None

    _runtime_token = _fake_runtime.activate(FakeCoordinator())
    try:
        await project_structure_edit_session._append_visible_turn(
            "review",
            "assistant-started",
            is_streaming=True,
            initial_assistant_content="started",
        )
    finally:
        _fake_runtime.deactivate(_runtime_token)

    messages = visible_session()["messages"]
    check(messages[1]["content"] == "started", "visible turn stores initial assistant content")
    check(messages[1]["isStreaming"] is True, "visible turn keeps initial assistant streaming")


async def _record_ack(acks: list[dict], msg: dict) -> None:
    acks.append(msg)


async def test_project_structure_submit_user_prompt_dedups_client_id() -> None:
    reset_state()
    project = make_project()
    await project_structure_edit_session.ensure_singleton(str(project))
    append_visible_message({
        "id": "user-1",
        "role": "user",
        "content": "apply",
        "timestamp": "now",
        "client_id": "edit-client-2",
    })
    enqueued: list[str] = []
    original_enqueue = project_structure_edit_session._enqueue_review
    project_structure_edit_session._enqueue_review = (
        lambda prompt, project_cwd, skill_dir, instructions=None, **kwargs: (
            enqueued.append(prompt) or "queued-followup"
        )
    )

    try:
        result = await project_structure_edit_session.submit_user_prompt(
            str(project),
            "apply",
            client_id="edit-client-2",
        )
    finally:
        project_structure_edit_session._enqueue_review = original_enqueue

    check(result["error"] == "duplicate_client_id", "duplicate project edit prompt is rejected")
    check(enqueued == [], "duplicate project edit prompt does not enqueue")


async def test_successful_review_marks_processed_updates_seen() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    first = project_update_store.append(project_id, "First update.")
    second = project_update_store.append(project_id, "Second update.")
    await project_structure_edit_session.ensure_singleton(str(project))
    broadcasts: list[tuple[str, dict]] = []

    class FakeCoordinator:
        async def _dispatch_messages_delta(self, root_id: str, sid: str, msg: dict, *, omit_render_events: bool = False):
            return None

        async def broadcast_global(self, event_type: str, data: dict):
            broadcasts.append((event_type, data))

    async def fake_dispatch(*args, **kwargs):
        return {"success": True, "sdk_output": "done"}

    _runtime_token = _fake_runtime.activate(FakeCoordinator())
    original_resolve_config = project_structure_edit_session.resolve_config
    original_ensure_session = project_structure_edit_session.ensure_session
    original_dispatch = project_structure_edit_session.dispatch
    original_extract_fork_text = project_structure_edit_session.extract_fork_text
    project_structure_edit_session.resolve_config = lambda _spec: ProvisionedConfig(
        cwd=str(project),
        model="claude-test",
        provider_id="provider-test",
        reasoning_effort="",
        run_mode="fork",
        dispatch="in_process",
        on_no_fork="error",
        node_id="primary",
        backend_url="http://localhost:8000",
        internal_token="",
        provisioned_session_id=None,
        caller_session_id=project_structure_edit_session.edit_singleton_id(),
        worker_description="project-structure-maintainer",
    )
    project_structure_edit_session.ensure_session = lambda spec, cfg: "base-session"
    project_structure_edit_session.dispatch = fake_dispatch
    project_structure_edit_session.extract_fork_text = lambda result: "done"

    try:
        await project_structure_edit_session._run_review(
            "assistant-seen",
            "review",
            str(project),
            str(project / ".agents" / "skills" / "project-structure"),
            "review",
            update_ids=[first["id"], second["id"]],
            client_id=None,
            lifecycle_msg_id=None,
            on_user_message=None,
        )
    finally:
        project_structure_edit_session.resolve_config = original_resolve_config
        project_structure_edit_session.ensure_session = original_ensure_session
        project_structure_edit_session.dispatch = original_dispatch
        project_structure_edit_session.extract_fork_text = original_extract_fork_text
        _fake_runtime.deactivate(_runtime_token)

    check(project_update_store.unseen_count(project_id) == 0, "successful review marks updates seen")
    check(
        broadcasts == [
            (
                "project_updates_changed",
                {"project_id": project_id, "unseen_count": 0},
            )
        ],
        "successful review broadcasts unseen count change",
    )


async def test_failed_review_keeps_updates_unseen() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    update = project_update_store.append(project_id, "Still pending.")
    await project_structure_edit_session.ensure_singleton(str(project))
    broadcasts: list[tuple[str, dict]] = []

    class FakeCoordinator:
        async def _dispatch_messages_delta(self, root_id: str, sid: str, msg: dict, *, omit_render_events: bool = False):
            return None

        async def broadcast_global(self, event_type: str, data: dict):
            broadcasts.append((event_type, data))

    async def fake_dispatch(*args, **kwargs):
        return {"success": False, "error": "failed"}

    _runtime_token = _fake_runtime.activate(FakeCoordinator())
    original_resolve_config = project_structure_edit_session.resolve_config
    original_ensure_session = project_structure_edit_session.ensure_session
    original_dispatch = project_structure_edit_session.dispatch
    original_logger_disabled = project_structure_edit_session.logger.disabled
    project_structure_edit_session.resolve_config = lambda _spec: ProvisionedConfig(
        cwd=str(project),
        model="claude-test",
        provider_id="provider-test",
        reasoning_effort="",
        run_mode="fork",
        dispatch="in_process",
        on_no_fork="error",
        node_id="primary",
        backend_url="http://localhost:8000",
        internal_token="",
        provisioned_session_id=None,
        caller_session_id=project_structure_edit_session.edit_singleton_id(),
        worker_description="project-structure-maintainer",
    )
    project_structure_edit_session.ensure_session = lambda spec, cfg: "base-session"
    project_structure_edit_session.dispatch = fake_dispatch
    project_structure_edit_session.logger.disabled = True

    try:
        await project_structure_edit_session._run_review(
            "assistant-failed",
            "review",
            str(project),
            str(project / ".agents" / "skills" / "project-structure"),
            "review",
            update_ids=[update["id"]],
            client_id=None,
            lifecycle_msg_id=None,
            on_user_message=None,
        )
    finally:
        project_structure_edit_session.resolve_config = original_resolve_config
        project_structure_edit_session.ensure_session = original_ensure_session
        project_structure_edit_session.dispatch = original_dispatch
        project_structure_edit_session.logger.disabled = original_logger_disabled
        _fake_runtime.deactivate(_runtime_token)

    check(project_update_store.unseen_count(project_id) == 1, "failed review keeps updates unseen")
    check(broadcasts == [], "failed review does not broadcast count change")


async def test_timed_out_review_keeps_updates_unseen_and_shows_error() -> None:
    reset_state()
    project = make_project()
    project_id = encode_cwd(str(project))
    update = project_update_store.append(project_id, "Still pending.")
    await project_structure_edit_session.ensure_singleton(str(project))
    broadcasts: list[tuple[str, dict]] = []

    class FakeCoordinator:
        async def _dispatch_messages_delta(self, root_id: str, sid: str, msg: dict, *, omit_render_events: bool = False):
            return None

        async def broadcast_global(self, event_type: str, data: dict):
            broadcasts.append((event_type, data))

    async def fake_dispatch(*args, **kwargs):
        await asyncio.Event().wait()
        return {"success": True, "sdk_output": "never"}

    _runtime_token = _fake_runtime.activate(FakeCoordinator())
    original_resolve_config = project_structure_edit_session.resolve_config
    original_ensure_session = project_structure_edit_session.ensure_session
    original_dispatch = project_structure_edit_session.dispatch
    original_timeout = project_structure_edit_session.MAINTAINER_REVIEW_TIMEOUT_SECONDS
    original_logger_disabled = project_structure_edit_session.logger.disabled
    project_structure_edit_session.resolve_config = lambda _spec: ProvisionedConfig(
        cwd=str(project),
        model="claude-test",
        provider_id="provider-test",
        reasoning_effort="",
        run_mode="fork",
        dispatch="in_process",
        on_no_fork="error",
        node_id="primary",
        backend_url="http://localhost:8000",
        internal_token="",
        provisioned_session_id=None,
        caller_session_id=project_structure_edit_session.edit_singleton_id(),
        worker_description="project-structure-maintainer",
    )
    project_structure_edit_session.ensure_session = lambda spec, cfg: "base-session"
    project_structure_edit_session.dispatch = fake_dispatch
    project_structure_edit_session.MAINTAINER_REVIEW_TIMEOUT_SECONDS = 0.01
    project_structure_edit_session.logger.disabled = True

    try:
        await project_structure_edit_session._run_review(
            "assistant-timeout",
            "review",
            str(project),
            str(project / ".agents" / "skills" / "project-structure"),
            "review",
            update_ids=[update["id"]],
            client_id=None,
            lifecycle_msg_id=None,
            on_user_message=None,
        )
    finally:
        project_structure_edit_session.resolve_config = original_resolve_config
        project_structure_edit_session.ensure_session = original_ensure_session
        project_structure_edit_session.dispatch = original_dispatch
        project_structure_edit_session.MAINTAINER_REVIEW_TIMEOUT_SECONDS = original_timeout
        project_structure_edit_session.logger.disabled = original_logger_disabled
        _fake_runtime.deactivate(_runtime_token)

    messages = visible_session()["messages"]
    assistant = next(msg for msg in messages if msg.get("id") == "assistant-timeout")
    check(project_update_store.unseen_count(project_id) == 1, "timed out review keeps updates unseen")
    check(broadcasts == [], "timed out review does not broadcast count change")
    check("timed out" in assistant["content"], "timed out review shows timeout message")
    check(assistant["isStreaming"] is False, "timed out review stops streaming")


def test_maintainer_spec_uses_provisioned_forks() -> None:
    spec = project_structure_edit_session.MAINTAINER_SPEC
    prompt = spec.build_provision_prompt({
        "project_cwd": "/repo",
        "skill_dir": "/repo/.claude/skills/project-structure",
    })
    check(spec.run_mode == "fork", "maintainer spec uses fork run mode")
    check(spec.ephemeral_forks is False, "maintainer spec reuses one persistent fork")
    check(spec.dispatch == "in_process", "maintainer spec uses provisioning dispatch")
    check(spec.machine_completion is False, "maintainer spec keeps tool-using prompt path")
    check("must never contradict user requirements" in prompt, "provision prompt has requirement guard")
    check("get-requirements" in prompt, "provision prompt requires requirements check")
    check("minimize user intervention" in prompt, "provision prompt minimizes user intervention")
    check("last resort" in prompt, "provision prompt asks user only as last resort")


async def test_maintainer_dispatch_uses_persistent_fork() -> None:
    reset_state()
    project = make_project()
    captured: list[dict] = []

    class FakeCoordinator:
        async def run_delegation(self, **kwargs):
            captured.append(kwargs)
            return {"success": True, "sdk_output": "ok"}

    _runtime_token = _fake_runtime.activate(FakeCoordinator())
    try:
        cfg = ProvisionedConfig(
            cwd=str(project),
            model="claude-test",
            provider_id="provider-test",
            reasoning_effort="",
            run_mode="fork",
            dispatch="in_process",
            on_no_fork="error",
            node_id="primary",
            backend_url="http://localhost:8000",
            internal_token="",
            provisioned_session_id=None,
            caller_session_id=project_structure_edit_session.edit_singleton_id(),
            worker_description="project-structure-maintainer",
        )
        await dispatch(
            project_structure_edit_session.MAINTAINER_SPEC,
            cfg,
            base_session_id="base-session",
            caller_session_id=project_structure_edit_session.edit_singleton_id(),
            instructions="apply",
            provision_prompt="ready",
        )
    finally:
        _fake_runtime.deactivate(_runtime_token)

    check(captured[0]["run_mode"] == "fork", "maintainer dispatch still uses fork mode")
    check(captured[0]["ephemeral"] is False, "maintainer dispatch persists fork")


def main() -> None:
    try:
        asyncio.run(test_submit_review_prompt_enqueues_full_params())
        asyncio.run(test_submit_review_prompt_no_unseen_does_not_submit())
        asyncio.run(test_submit_review_prompt_reuses_active_review())
        asyncio.run(test_concurrent_submit_review_prompt_enqueues_once())
        asyncio.run(test_find_skill_dir_prefers_agents_over_claude())
        test_maintainer_dirty_policy_accepts_valid_provision_shape()
        test_maintainer_ensure_session_reuses_valid_provisioned_base()
        asyncio.run(test_prepare_review_prompt_does_not_enqueue())
        asyncio.run(test_prepare_review_prompt_supports_agents_skill_dir())
        asyncio.run(test_ensure_singleton_refreshes_project_cwd_meta())
        asyncio.run(test_submit_user_prompt_missing_skill_dir_fails_without_visible_turn())
        asyncio.run(test_submit_user_prompt_routes_followup_through_provisioned_enqueue())
        asyncio.run(test_project_structure_visible_turn_acks_user_message())
        asyncio.run(test_project_structure_visible_turn_can_show_initial_assistant_content())
        asyncio.run(test_project_structure_submit_user_prompt_dedups_client_id())
        asyncio.run(test_successful_review_marks_processed_updates_seen())
        asyncio.run(test_failed_review_keeps_updates_unseen())
        asyncio.run(test_timed_out_review_keeps_updates_unseen_and_shows_error())
        test_maintainer_spec_uses_provisioned_forks()
        asyncio.run(test_maintainer_dispatch_uses_persistent_fork())
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()

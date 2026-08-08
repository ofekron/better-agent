"""Backend unit tests for assistant session provisioning.

Covers the two provisioning bugs fixed together:
  * `build_capability_contexts` must emit one `outputs` entry per provider
    kind — the runner's `provider_capability_contexts` filters by
    `provider_kind`, and a context with no matching output (the old
    `{name, category, content}` shape) is silently dropped, so the role
    prompt never reached the assistant session.
  * A `name_locked` session refuses rename from every path (AI auto-title,
    first-prompt auto-name, user rename) — all funnel through
    `session_manager.rename`.

Run with:
    cd backend && .venv/bin/python scripts/test_assistant_provisioning.py
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module. `isolate_installed` commits a real active
# installation profile: session_manager.create() funnels team/manager mode
# through installation_profile.assert_orchestration_mode_allowed, which
# requires provider_conversations + integrations to be bootstrap-ready.
import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-assistant-provisioning-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ensure_singleton/ensure_monitor resolve the assistant core-role extension
# owner (_ext_id) before provisioning; register it in the isolated home.
import _test_extension  # noqa: E402
_test_extension.install_core_role(Path(_TMP_HOME), "assistant")

# A second configured provider, distinct from the seeded default, so the
# stale-pointer repair test can prove ensure_singleton preserves an existing
# session's selectors instead of overwriting them with the default.
import config_store as _config_store  # noqa: E402
KEPT_PROVIDER_ID = _config_store.add_provider({"kind": "claude", "name": "Kept"})["id"]

import assistant_ui  # noqa: E402
import capability_contexts  # noqa: E402
import working_mode  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

_ROLE_PROMPT = "# Assistant\n\nYou are the user's single point of contact."


def _reset_role_sessions() -> None:
    """Hermetic reset between ensure_* tests.

    The module shares one isolated home, and ensure_singleton/ensure_monitor
    persist role sessions + assistant_singleton state. cleanup_singleton only
    removes sessions referenced by current state, and the sidebar `list()`
    summary index is debounced and excludes hidden (monitor) sessions — so
    iterating `list()` misses just-created or hidden role sessions. Walk the
    authoritative root-session set instead, drop every extension-source
    session, and clear state so each test starts clean.
    """
    assistant_ui.cleanup_singleton()
    for sess in session_manager.iter_root_sessions():
        if sess.get("source") == "extension":
            session_manager.delete(sess["id"])
    assistant_ui._state_path().unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────
# capability_contexts shape — provider delivery
# ──────────────────────────────────────────────────────────────────────


def test_capability_contexts_deliver_per_provider() -> None:
    """The role prompt must survive `normalize_capability_contexts` and be
    selected by `provider_capability_contexts` for each conversation
    provider kind. Before the fix the shape had no `outputs`, so nothing
    was delivered."""
    original = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: _ROLE_PROMPT  # type: ignore[assignment]
    try:
        caps = assistant_ui.build_capability_contexts(board_preamble="<board>x</board>")
    finally:
        assistant_ui._system_prompt = original  # type: ignore[assignment]

    assert len(caps) == 1, f"expected 1 capability context, got {len(caps)}"
    ctx = caps[0]
    assert isinstance(ctx.get("outputs"), list) and ctx["outputs"], f"no outputs list: {ctx!r}"

    normalized = capability_contexts.normalize_capability_contexts(caps)
    assert len(normalized) == 1, f"normalized to {len(normalized)} contexts (want 1)"

    kinds = {o["provider_kind"] for o in ctx["outputs"]}
    for required in ("claude", "codex", "openai", "agy", "fugu",
                     "claude-remote", "copilot"):
        assert required in kinds, f"missing output for provider_kind {required!r}"

    # The runner selects by provider_kind — every provider must get the role
    # prompt + preamble. This is the exact bug: pre-fix it was [].
    for kind in ("claude", "codex", "openai", "agy", "fugu",
                 "claude-remote", "copilot"):
        selected = capability_contexts.provider_capability_contexts(caps, kind)
        assert len(selected) == 1, f"provider {kind!r} got {len(selected)} contexts (want 1)"
        content = selected[0].get("content") or ""
        assert _ROLE_PROMPT in content and "<board>x</board>" in content, (
            f"provider {kind!r} content missing role/preamble: {content!r}"
        )

    # Empty content -> no context (the cached prefix stays empty, not malformed).
    assert assistant_ui.build_capability_contexts(board_preamble="") == []
    original2 = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: "   "  # type: ignore[assignment]
    try:
        # Whitespace-only prompt with no preamble still yields no context.
        assert assistant_ui.build_capability_contexts(board_preamble="") == []
    finally:
        assistant_ui._system_prompt = original2  # type: ignore[assignment]


def test_capability_contexts_hash_is_order_stable() -> None:
    """The capability_contexts hash must be byte-stable regardless of the
    order providers load in, so ensure_singleton doesn't churn the cached
    prompt prefix."""
    original = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: _ROLE_PROMPT  # type: ignore[assignment]
    import provider as provider_mod
    real_known = provider_mod.known_providers

    class _P:
        def __init__(self, kind):
            self.KIND = kind

    try:
        caps_a = assistant_ui.build_capability_contexts(board_preamble="")
        caps_b = assistant_ui.build_capability_contexts(board_preamble="")
        h1 = assistant_ui._caps_hash(caps_a)
        h2 = assistant_ui._caps_hash(caps_b)
        # Set-stability: even if the live registry returns a varying subset, the
        # merged set is anchored by the comprehensive fallback → same hash.
        provider_mod.known_providers = lambda: [_P("claude"), _P("codex")]  # type: ignore[assignment]
        h_subset = assistant_ui._caps_hash(assistant_ui.build_capability_contexts(board_preamble=""))
    finally:
        assistant_ui._system_prompt = original  # type: ignore[assignment]
        provider_mod.known_providers = real_known  # type: ignore[assignment]

    assert h1 == h2, f"caps hash not stable: {h1} != {h2}"
    assert h_subset == h1, f"caps hash changed with registry subset (cache churn): {h_subset} != {h1}"


def test_capability_contexts_content_is_bounded() -> None:
    """The internal build path bypasses normalize_capability_contexts, so the
    content cap must be enforced in build — a runaway board_preamble can't grow
    the cached prefix without bound."""
    original = assistant_ui._system_prompt
    big_preamble = "x" * (capability_contexts.MAX_CAPABILITY_CONTENT_CHARS + 5000)
    assistant_ui._system_prompt = lambda: ""  # type: ignore[assignment]
    try:
        caps = assistant_ui.build_capability_contexts(board_preamble=big_preamble)
    finally:
        assistant_ui._system_prompt = original  # type: ignore[assignment]
    assert caps, "bounded build returned no contexts"
    content = caps[0]["outputs"][0]["content"]
    assert len(content) <= capability_contexts.MAX_CAPABILITY_CONTENT_CHARS, (
        f"content not capped: {len(content)} > {capability_contexts.MAX_CAPABILITY_CONTENT_CHARS}"
    )
    # The capped shape must still pass the REST validator.
    capability_contexts.normalize_capability_contexts(caps)


# ──────────────────────────────────────────────────────────────────────
# name_locked — rename refused from every path
# ──────────────────────────────────────────────────────────────────────


def test_rename_refused_when_locked() -> None:
    """A name_locked session keeps its name through rename() — the single
    funnel used by AI auto-title, first-prompt auto-name, and the user
    rename endpoint."""
    sess = session_manager.create(name="Assistant", cwd=str(_TMP_HOME), source="test")
    sid = sess["id"]
    session_manager.set_name_locked(sid, True)

    out = session_manager.rename(sid, "Renamed by AI")
    assert out is not None, "rename returned None for a locked session (ambiguous with not-found)"
    after = session_manager.get_lite(sid)
    assert after.get("name") == "Assistant", f"locked session was renamed to {after.get('name')!r}"
    assert after.get("name_locked"), "name_locked flag did not persist"

    # An unlocked session renames normally.
    other = session_manager.create(name="Work", cwd=str(_TMP_HOME), source="test")
    session_manager.rename(other["id"], "Renamed")
    assert session_manager.get_lite(other["id"]).get("name") == "Renamed", "unlocked session did not rename"


def test_rename_force_overrides_lock() -> None:
    """The owner can restore the canonical name on a locked session via
    force=True — the internal escape hatch ensure_singleton uses to self-heal
    a singleton renamed before the lock existed."""
    sess = session_manager.create(name="Drifted", cwd=str(_TMP_HOME), source="test")
    sid = sess["id"]
    session_manager.set_name_locked(sid, True)
    out = session_manager.rename(sid, "Assistant", force=True)
    after = session_manager.get_lite(sid)
    assert after.get("name") == "Assistant", f"force rename did not apply: name={after.get('name')!r}"
    assert after.get("name_locked"), "force rename dropped the name_locked flag"
    assert out is not None, "force rename returned None"


def test_rename_strips_embedded_link_marker_syntax() -> None:
    """A session name is never allowed to persist raw `[[ba-session:...]]` /
    `[[ba-event:...]]` copy-id marker syntax — every marker builder
    (frontend Copy id, team_messaging's FROM tag) re-embeds the stored name
    inside a NEW marker, and a name that's already one renders as garbled
    nested/percent-encoded garbage. This funnels through rename() for both
    the user rename endpoint and the AI auto-title path."""
    sess = session_manager.create(name="Work", cwd=str(_TMP_HOME), source="test")
    sid = sess["id"]

    polluted = "[[ba-session:558946c1-d081-4e8b-88d4-96aab0fa68aa|Old Session]]"
    out = session_manager.rename(sid, polluted)
    assert out is not None, "rename returned None"
    after = session_manager.get_lite(sid)
    assert after.get("name") == "", f"embedded marker syntax was not stripped: name={after.get('name')!r}"

    # A name with real text alongside a marker keeps the real text.
    session_manager.rename(sid, "Prefix [[ba-event:a|b|c]] Suffix")
    after2 = session_manager.get_lite(sid)
    assert "[[ba-event:" not in (after2.get("name") or ""), (
        f"marker syntax survived alongside real text: name={after2.get('name')!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# ensure_singleton / ensure_monitor provisioning
# ──────────────────────────────────────────────────────────────────────


def test_ensure_singleton_is_user_visible() -> None:
    _reset_role_sessions()
    original_prompt = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: ""  # type: ignore[assignment]
    try:
        hidden = session_manager.create(
            name="Assistant",
            cwd=str(_TMP_HOME),
            source="extension",
            user_initiated=False,
        )
        assistant_ui._write_state({"session_id": hidden["id"]})
        healed = assistant_ui.ensure_singleton()
        assert healed.get("source") == "extension", f"ensure_singleton source={healed.get('source')!r}"
        assert healed.get("user_initiated") is True, (
            f"ensure_singleton user_initiated={healed.get('user_initiated')!r}"
        )
        assert healed.get("name_locked"), "ensure_singleton did not lock the canonical name"
    finally:
        assistant_ui._system_prompt = original_prompt  # type: ignore[assignment]


def test_ensure_monitor_is_hidden_and_separate() -> None:
    _reset_role_sessions()
    original_prompt = assistant_ui._monitor_prompt
    assistant_ui._monitor_prompt = lambda: "# Assistant Monitor"  # type: ignore[assignment]
    try:
        monitor = assistant_ui.ensure_monitor("board")
        visible = assistant_ui.ensure_singleton("board")
        summary = next(s for s in session_manager.list() if s["id"] == monitor["id"])
        state = assistant_ui._read_state()
        assert monitor.get("id") != visible.get("id"), "monitor reused visible assistant id"
        assert monitor.get("name") == assistant_ui.MONITOR_NAME, f"monitor name={monitor.get('name')!r}"
        assert monitor.get("user_initiated") is False, (
            f"monitor user_initiated={monitor.get('user_initiated')!r}"
        )
        assert monitor.get("working_mode") == assistant_ui.MONITOR_WORKING_MODE, (
            f"monitor working_mode={monitor.get('working_mode')!r}"
        )
        assert working_mode.should_hide_from_sidebar(summary), "monitor summary is not sidebar-hidden"
        assert state.get("monitor_session_id") == monitor["id"], (
            f"monitor state id={state.get('monitor_session_id')!r}"
        )
        assert monitor.get("name_locked"), "monitor name is not locked"
        selected = capability_contexts.provider_capability_contexts(
            monitor.get("capability_contexts") or [],
            "claude",
        )
        content = selected[0].get("content") if selected else ""
        assert "Assistant Monitor" in content and "board" in content, (
            f"monitor role/preamble missing: {content!r}"
        )
    finally:
        assistant_ui._monitor_prompt = original_prompt  # type: ignore[assignment]


def test_ensure_activates_role_capability() -> None:
    """The MCP manifest gates each role's gated MCP server on a `contains:
    {active_capability_ids: ...}` predicate. ensure_* must activate that id on
    the session record, not just deliver the role prompt via capability_contexts
    — otherwise the gated MCP (singleton tools; monitor's set_assistant_status)
    never launches. Regression lock for the DOA status write-path."""
    _reset_role_sessions()
    orig_sys, orig_mon = assistant_ui._system_prompt, assistant_ui._monitor_prompt
    assistant_ui._system_prompt = lambda: "# Assistant"  # type: ignore[assignment]
    assistant_ui._monitor_prompt = lambda: "# Assistant Monitor"  # type: ignore[assignment]
    try:
        singleton = assistant_ui.ensure_singleton("board")
        monitor = assistant_ui.ensure_monitor("board")
        s_rec = session_manager.get(singleton["id"]) or {}
        m_rec = session_manager.get(monitor["id"]) or {}
        assert "assistant-role" in (s_rec.get("active_capability_ids") or []), (
            f"singleton active_capability_ids={s_rec.get('active_capability_ids')!r}"
        )
        assert "assistant-monitor-role" in (m_rec.get("active_capability_ids") or []), (
            f"monitor active_capability_ids={m_rec.get('active_capability_ids')!r}"
        )
        # Idempotent: re-ensuring must not duplicate the id.
        assistant_ui.ensure_singleton("board")
        again = session_manager.get(singleton["id"]) or {}
        assert (again.get("active_capability_ids") or []).count("assistant-role") == 1, (
            f"re-ensure duplicated the capability id: {again.get('active_capability_ids')!r}"
        )
    finally:
        assistant_ui._system_prompt = orig_sys  # type: ignore[assignment]
        assistant_ui._monitor_prompt = orig_mon  # type: ignore[assignment]


def test_ensure_singleton_repairs_capability_context_drift() -> None:
    _reset_role_sessions()
    original_prompt = assistant_ui._system_prompt
    try:
        assistant_ui._system_prompt = lambda: "# Canonical Assistant"  # type: ignore[assignment]
        singleton = assistant_ui.ensure_singleton("board")
        sid = singleton["id"]
        session_manager.set_capability_contexts(sid, [])

        repaired = assistant_ui.ensure_singleton("board")
        repaired_caps = repaired.get("capability_contexts") or []
        assert any(
            "Canonical Assistant" in str(output.get("content") or "")
            for cap in repaired_caps
            for output in cap.get("outputs") or []
        ), f"ensure_singleton did not repair missing contexts: {repaired_caps!r}"

        session_manager.set_capability_contexts(sid, [{"capability_id": "stale", "content": "stale"}])
        assistant_ui._system_prompt = lambda: ""  # type: ignore[assignment]
        cleared = assistant_ui.ensure_singleton("")
        assert cleared.get("capability_contexts") == [], (
            f"ensure_singleton did not clear stale contexts: {cleared.get('capability_contexts')!r}"
        )
    finally:
        assistant_ui._system_prompt = original_prompt  # type: ignore[assignment]


def test_ensure_singleton_repairs_stale_pointer_without_duplicate() -> None:
    _reset_role_sessions()
    original_prompt = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: ""  # type: ignore[assignment]
    try:
        existing = session_manager.create(
            name="Assistant",
            model="kept-model",
            provider_id=KEPT_PROVIDER_ID,
            cwd=str(_TMP_HOME),
            source="extension",
            user_initiated=True,
            created_at="2026-01-01T00:00:00",
        )
        session_manager.set_name_locked(existing["id"], True)
        assistant_ui._write_state({"session_id": "missing-assistant"})

        healed = assistant_ui.ensure_singleton()
        # Count authoritatively (iter_root_sessions): the sidebar `list()`
        # summary index retains ghost entries for deleted sessions, so it
        # overcounts right after the per-test reset deletes prior sessions.
        assistants = [
            sess for sess in session_manager.iter_root_sessions()
            if sess.get("source") == "extension" and sess.get("name") == "Assistant"
        ]
        state = assistant_ui._read_state()
        assert healed.get("id") == existing["id"], (
            f"stale pointer healed to {healed.get('id')!r}, want {existing['id']!r}"
        )
        assert len(assistants) == 1, f"stale pointer created {len(assistants)} Assistant sessions"
        assert state.get("session_id") == existing["id"], (
            f"stale pointer state repaired to {state.get('session_id')!r}"
        )
        assert healed.get("provider_id") == KEPT_PROVIDER_ID and healed.get("model") == "kept-model", (
            f"healed singleton changed selectors: {healed.get('provider_id')!r}/{healed.get('model')!r}"
        )
    finally:
        _reset_role_sessions()
        assistant_ui._system_prompt = original_prompt  # type: ignore[assignment]


def test_ensure_singleton_chooses_oldest_existing_duplicate() -> None:
    _reset_role_sessions()
    original_prompt = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: ""  # type: ignore[assignment]
    try:
        older = session_manager.create(
            id="assistant-existing-old",
            name="Assistant",
            cwd=str(_TMP_HOME),
            source="extension",
            user_initiated=True,
            created_at="2026-01-01T00:00:00",
        )
        newer = session_manager.create(
            id="assistant-existing-new",
            name="Assistant",
            cwd=str(_TMP_HOME),
            source="extension",
            user_initiated=True,
            created_at="2026-02-01T00:00:00",
        )
        assistant_ui._write_state({"session_id": "missing-assistant"})

        healed = assistant_ui.ensure_singleton()
        assert healed.get("id") == older["id"], (
            f"duplicate resolver chose {healed.get('id')!r}, want {older['id']!r}"
        )
        assert assistant_ui._read_state().get("session_id") == older["id"], (
            "duplicate resolver did not repair state to oldest id"
        )
        assert session_manager.get(newer["id"]) is not None, (
            "duplicate resolver deleted an existing session"
        )
    finally:
        _reset_role_sessions()
        assistant_ui._system_prompt = original_prompt  # type: ignore[assignment]


def main_run() -> int:
    tests = [
        test_capability_contexts_deliver_per_provider,
        test_capability_contexts_hash_is_order_stable,
        test_capability_contexts_content_is_bounded,
        test_rename_refused_when_locked,
        test_rename_force_overrides_lock,
        test_rename_strips_embedded_link_marker_syntax,
        test_ensure_singleton_is_user_visible,
        test_ensure_monitor_is_hidden_and_separate,
        test_ensure_activates_role_capability,
        test_ensure_singleton_repairs_capability_context_drift,
        test_ensure_singleton_repairs_stale_pointer_without_duplicate,
        test_ensure_singleton_chooses_oldest_existing_duplicate,
    ]
    n_pass = 0
    for fn in tests:
        try:
            fn()
            n_pass += 1
        except Exception:
            traceback.print_exc()
            print(f"FAIL {fn.__name__}")
    print(f"\n{n_pass}/{len(tests)} assistant provisioning tests passed")
    return 0 if n_pass == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main_run())

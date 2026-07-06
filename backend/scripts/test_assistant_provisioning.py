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
import shutil
import sys
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-assistant-provisioning-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import assistant_ui  # noqa: E402
import capability_contexts  # noqa: E402
import working_mode  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_ROLE_PROMPT = "# Assistant\n\nYou are the user's single point of contact."


# ──────────────────────────────────────────────────────────────────────
# capability_contexts shape — provider delivery
# ──────────────────────────────────────────────────────────────────────


def test_capability_contexts_deliver_per_provider() -> bool:
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

    ok = True
    if len(caps) != 1:
        print(f"{FAIL} expected 1 capability context, got {len(caps)}")
        return False
    ctx = caps[0]
    if not isinstance(ctx.get("outputs"), list) or not ctx["outputs"]:
        print(f"{FAIL} context has no outputs list: {ctx!r}")
        return False

    # Must pass the validator the REST layer applies.
    try:
        normalized = capability_contexts.normalize_capability_contexts(caps)
    except ValueError as exc:
        print(f"{FAIL} normalize rejected the shape: {exc}")
        return False
    if len(normalized) != 1:
        print(f"{FAIL} normalized to {len(normalized)} contexts (want 1)")
        return False

    kinds = {o["provider_kind"] for o in ctx["outputs"]}
    # Every provider KIND the assistant can run on must be covered — a missing
    # output means that provider silently gets no role prompt (the original bug).
    for required in ("claude", "codex", "gemini", "openai", "agy", "fugu",
                     "claude-remote", "copilot"):
        if required not in kinds:
            print(f"{FAIL} missing output for provider_kind {required!r}")
            ok = False

    # The runner selects by provider_kind — every provider must get the role
    # prompt + preamble. This is the exact bug: pre-fix it was [].
    for kind in ("claude", "codex", "gemini", "openai", "agy", "fugu",
                 "claude-remote", "copilot"):
        selected = capability_contexts.provider_capability_contexts(caps, kind)
        if len(selected) != 1:
            print(f"{FAIL} provider {kind!r} got {len(selected)} contexts (want 1)")
            ok = False
            continue
        content = selected[0].get("content") or ""
        if _ROLE_PROMPT not in content or "<board>x</board>" not in content:
            print(f"{FAIL} provider {kind!r} content missing role/preamble: {content!r}")
            ok = False

    # Empty content -> no context (the cached prefix stays empty, not malformed).
    if assistant_ui.build_capability_contexts(board_preamble="") != []:
        # Only holds when the system prompt is empty; force that here.
        original2 = assistant_ui._system_prompt
        assistant_ui._system_prompt = lambda: "   "  # type: ignore[assignment]
        try:
            if assistant_ui.build_capability_contexts(board_preamble="x") != []:
                print(f"{FAIL} empty prompt should yield no contexts")
                ok = False
        finally:
            assistant_ui._system_prompt = original2  # type: ignore[assignment]

    if ok:
        print(f"{PASS} capability_contexts deliver the role prompt per provider")
    return ok


def test_capability_contexts_hash_is_order_stable() -> bool:
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
        # merged set is anchored by the comprehensive fallback → same hash. This
        # locks the cache-churn fix.
        provider_mod.known_providers = lambda: [_P("claude"), _P("codex")]  # type: ignore[assignment]
        h_subset = assistant_ui._caps_hash(assistant_ui.build_capability_contexts(board_preamble=""))
    finally:
        assistant_ui._system_prompt = original  # type: ignore[assignment]
        provider_mod.known_providers = real_known  # type: ignore[assignment]
    if h1 != h2:
        print(f"{FAIL} caps hash not stable: {h1} != {h2}")
        return False
    if h_subset != h1:
        print(f"{FAIL} caps hash changed with registry subset (cache churn): {h_subset} != {h1}")
        return False
    print(f"{PASS} capability_contexts hash is order- and set-stable")
    return True


def test_capability_contexts_content_is_bounded() -> bool:
    """The internal build path bypasses normalize_capability_contexts, so the
    content cap must be enforced in build — a runaway board_preamble can't grow
    the cached prefix without bound."""
    original = assistant_ui._system_prompt
    big_preamble = "x" * (capability_contexts.MAX_CAPABILITY_CONTENT_CHARS + 5000)
    assistant_ui._system_prompt = lambda: ""  # preamble-only content
    try:
        caps = assistant_ui.build_capability_contexts(board_preamble=big_preamble)
    finally:
        assistant_ui._system_prompt = original  # type: ignore[assignment]
    if not caps:
        print(f"{FAIL} bounded build returned no contexts")
        return False
    content = caps[0]["outputs"][0]["content"]
    if len(content) > capability_contexts.MAX_CAPABILITY_CONTENT_CHARS:
        print(f"{FAIL} content not capped: {len(content)} > {capability_contexts.MAX_CAPABILITY_CONTENT_CHARS}")
        return False
    # The capped shape must still pass the REST validator.
    try:
        capability_contexts.normalize_capability_contexts(caps)
    except ValueError as exc:
        print(f"{FAIL} capped caps rejected by normalize: {exc}")
        return False
    print(f"{PASS} capability_contexts content is capped to the bound")
    return True


# ──────────────────────────────────────────────────────────────────────
# name_locked — rename refused from every path
# ──────────────────────────────────────────────────────────────────────


def test_rename_refused_when_locked() -> bool:
    """A name_locked session keeps its name through rename() — the single
    funnel used by AI auto-title, first-prompt auto-name, and the user
    rename endpoint."""
    sess = session_manager.create(name="Assistant", cwd=str(_TMP_HOME), source="test")
    sid = sess["id"]
    session_manager.set_name_locked(sid, True)

    out = session_manager.rename(sid, "Renamed by AI")
    ok = True
    if out is None:
        print(f"{FAIL} rename returned None for a locked session (ambiguous with not-found)")
        ok = False
    after = session_manager.get_lite(sid)
    if after.get("name") != "Assistant":
        print(f"{FAIL} locked session was renamed to {after.get('name')!r}")
        ok = False
    if not after.get("name_locked"):
        print(f"{FAIL} name_locked flag did not persist")
        ok = False

    # An unlocked session renames normally.
    other = session_manager.create(name="Work", cwd=str(_TMP_HOME), source="test")
    session_manager.rename(other["id"], "Renamed")
    if session_manager.get_lite(other["id"]).get("name") != "Renamed":
        print(f"{FAIL} unlocked session did not rename")
        ok = False

    if ok:
        print(f"{PASS} name_locked session refuses rename; unlocked renames")
    return ok


def test_rename_force_overrides_lock() -> bool:
    """The owner can restore the canonical name on a locked session via
    force=True — the internal escape hatch ensure_singleton uses to self-heal
    a singleton renamed before the lock existed."""
    sess = session_manager.create(name="Drifted", cwd=str(_TMP_HOME), source="test")
    sid = sess["id"]
    session_manager.set_name_locked(sid, True)
    out = session_manager.rename(sid, "Assistant", force=True)
    after = session_manager.get_lite(sid)
    ok = True
    if after.get("name") != "Assistant":
        print(f"{FAIL} force rename did not apply: name={after.get('name')!r}")
        ok = False
    if not after.get("name_locked"):
        print(f"{FAIL} force rename dropped the name_locked flag")
        ok = False
    if out is None:
        print(f"{FAIL} force rename returned None")
        ok = False
    if ok:
        print(f"{PASS} rename(force=True) overrides the lock (keeps flag)")
    return ok


def test_ensure_singleton_is_user_visible() -> bool:
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
        ok = True
        if healed.get("source") != "extension":
            print(f"{FAIL} ensure_singleton source={healed.get('source')!r}")
            ok = False
        if healed.get("user_initiated") is not True:
            print(f"{FAIL} ensure_singleton user_initiated={healed.get('user_initiated')!r}")
            ok = False
        if not healed.get("name_locked"):
            print(f"{FAIL} ensure_singleton did not lock the canonical name")
            ok = False
        if ok:
            print(f"{PASS} ensure_singleton creates/heals a user-visible Assistant session")
        return ok
    finally:
        assistant_ui._system_prompt = original_prompt  # type: ignore[assignment]


def test_ensure_monitor_is_hidden_and_separate() -> bool:
    original_prompt = assistant_ui._monitor_prompt
    assistant_ui._monitor_prompt = lambda: "# Assistant Monitor"  # type: ignore[assignment]
    try:
        monitor = assistant_ui.ensure_monitor("board")
        visible = assistant_ui.ensure_singleton("board")
        summary = next(s for s in session_manager.list() if s["id"] == monitor["id"])
        state = assistant_ui._read_state()
        ok = True
        if monitor.get("id") == visible.get("id"):
            print(f"{FAIL} monitor reused visible assistant id")
            ok = False
        if monitor.get("name") != assistant_ui.MONITOR_NAME:
            print(f"{FAIL} monitor name={monitor.get('name')!r}")
            ok = False
        if monitor.get("user_initiated") is not False:
            print(f"{FAIL} monitor user_initiated={monitor.get('user_initiated')!r}")
            ok = False
        if monitor.get("working_mode") != assistant_ui.MONITOR_WORKING_MODE:
            print(f"{FAIL} monitor working_mode={monitor.get('working_mode')!r}")
            ok = False
        if not working_mode.should_hide_from_sidebar(summary):
            print(f"{FAIL} monitor summary is not sidebar-hidden")
            ok = False
        if state.get("monitor_session_id") != monitor["id"]:
            print(f"{FAIL} monitor state id={state.get('monitor_session_id')!r}")
            ok = False
        if not monitor.get("name_locked"):
            print(f"{FAIL} monitor name is not locked")
            ok = False
        selected = capability_contexts.provider_capability_contexts(
            monitor.get("capability_contexts") or [],
            "claude",
        )
        content = selected[0].get("content") if selected else ""
        if "Assistant Monitor" not in content or "board" not in content:
            print(f"{FAIL} monitor role/preamble missing: {content!r}")
            ok = False
        if ok:
            print(f"{PASS} ensure_monitor creates a hidden monitor separate from Assistant")
        return ok
    finally:
        assistant_ui._monitor_prompt = original_prompt  # type: ignore[assignment]


def test_ensure_singleton_repairs_stale_pointer_without_duplicate() -> bool:
    original_prompt = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: ""  # type: ignore[assignment]
    assistant_ui.cleanup_singleton()
    existing_id = ""
    try:
        existing = session_manager.create(
            name="Assistant",
            model="kept-model",
            provider_id="kept-provider",
            cwd=str(_TMP_HOME),
            source="extension",
            user_initiated=True,
            created_at="2026-01-01T00:00:00",
        )
        existing_id = existing["id"]
        session_manager.set_name_locked(existing["id"], True)
        assistant_ui._write_state({"session_id": "missing-assistant"})

        healed = assistant_ui.ensure_singleton()
        assistants = [
            sess for sess in session_manager.list()
            if sess.get("source") == "extension" and sess.get("name") == "Assistant"
        ]
        state = assistant_ui._read_state()
        ok = True
        if healed.get("id") != existing["id"]:
            print(f"{FAIL} stale pointer healed to {healed.get('id')!r}, want {existing['id']!r}")
            ok = False
        if len(assistants) != 1:
            print(f"{FAIL} stale pointer created {len(assistants)} Assistant sessions")
            ok = False
        if state.get("session_id") != existing["id"]:
            print(f"{FAIL} stale pointer state repaired to {state.get('session_id')!r}")
            ok = False
        if healed.get("provider_id") != "kept-provider" or healed.get("model") != "kept-model":
            print(f"{FAIL} healed singleton changed selectors: {healed.get('provider_id')!r}/{healed.get('model')!r}")
            ok = False
        if ok:
            print(f"{PASS} ensure_singleton repairs stale state without creating a duplicate")
        return ok
    finally:
        if existing_id:
            session_manager.delete(existing_id)
        assistant_ui._state_path().unlink(missing_ok=True)
        assistant_ui._system_prompt = original_prompt  # type: ignore[assignment]


def test_ensure_singleton_chooses_oldest_existing_duplicate() -> bool:
    original_prompt = assistant_ui._system_prompt
    assistant_ui._system_prompt = lambda: ""  # type: ignore[assignment]
    assistant_ui.cleanup_singleton()
    created_ids: list[str] = []
    try:
        older = session_manager.create(
            id="assistant-existing-old",
            name="Assistant",
            cwd=str(_TMP_HOME),
            source="extension",
            user_initiated=True,
            created_at="2026-01-01T00:00:00",
        )
        created_ids.append(older["id"])
        newer = session_manager.create(
            id="assistant-existing-new",
            name="Assistant",
            cwd=str(_TMP_HOME),
            source="extension",
            user_initiated=True,
            created_at="2026-02-01T00:00:00",
        )
        created_ids.append(newer["id"])
        assistant_ui._write_state({"session_id": "missing-assistant"})

        healed = assistant_ui.ensure_singleton()
        ok = True
        if healed.get("id") != older["id"]:
            print(f"{FAIL} duplicate resolver chose {healed.get('id')!r}, want {older['id']!r}")
            ok = False
        if assistant_ui._read_state().get("session_id") != older["id"]:
            print(f"{FAIL} duplicate resolver did not repair state to oldest id")
            ok = False
        if session_manager.get(newer["id"]) is None:
            print(f"{FAIL} duplicate resolver deleted an existing session")
            ok = False
        if ok:
            print(f"{PASS} ensure_singleton deterministically chooses oldest existing Assistant")
        return ok
    finally:
        for sid in created_ids:
            session_manager.delete(sid)
        assistant_ui._state_path().unlink(missing_ok=True)
        assistant_ui._system_prompt = original_prompt  # type: ignore[assignment]


def main_run() -> int:
    tests = [
        test_capability_contexts_deliver_per_provider,
        test_capability_contexts_hash_is_order_stable,
        test_capability_contexts_content_is_bounded,
        test_rename_refused_when_locked,
        test_rename_force_overrides_lock,
        test_ensure_singleton_is_user_visible,
        test_ensure_monitor_is_hidden_and_separate,
        test_ensure_singleton_repairs_stale_pointer_without_duplicate,
        test_ensure_singleton_chooses_oldest_existing_duplicate,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print(f"\n{n_pass}/{n_total} assistant provisioning tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())

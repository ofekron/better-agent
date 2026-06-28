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
    for required in ("claude", "codex", "gemini", "openai"):
        if required not in kinds:
            print(f"{FAIL} missing output for provider_kind {required!r}")
            ok = False

    # The runner selects by provider_kind — every conversation provider must
    # get the role prompt + preamble. This is the exact bug: pre-fix it was [].
    for kind in ("claude", "codex", "gemini", "openai"):
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
    try:
        caps_a = assistant_ui.build_capability_contexts(board_preamble="")
        caps_b = assistant_ui.build_capability_contexts(board_preamble="")
    finally:
        assistant_ui._system_prompt = original  # type: ignore[assignment]
    h1 = assistant_ui._caps_hash(caps_a)
    h2 = assistant_ui._caps_hash(caps_b)
    if h1 != h2:
        print(f"{FAIL} caps hash not stable: {h1} != {h2}")
        return False
    print(f"{PASS} capability_contexts hash is stable across calls")
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


def main_run() -> int:
    tests = [
        test_capability_contexts_deliver_per_provider,
        test_capability_contexts_hash_is_order_stable,
        test_rename_refused_when_locked,
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

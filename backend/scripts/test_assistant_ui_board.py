"""Backend unit tests for the assistant board-update fork (assistant_ui).

Covers the deterministic pieces that don't need a live claude subprocess:
  * `_parse_board_json` — last-balanced-object extraction + non-dict / junk
    handling.
  * `AssistantBoardSpec` shape — fork run-mode / in-process dispatch /
    bare+machine_completion / registration / provision-prompt is stateless
    (no item state baked into the cached base) / per-fork instruction is the
    identity payload.
  * `_normalize_classifications` — `id`↔`turn_id` aliasing, bad-row dropping.
  * `classify` / `extract_status` / `rank` — instruction shape + the
    parse/normalize path, with `provisioning.run` monkeypatched (no LLM).
  * `rank` order hygiene — dedupe, drop-unknown, never-lose-an-item.
  * Board endpoints reject malformed bodies before any fork.

Run with:
    cd backend && .venv/bin/python scripts/test_assistant_ui_board.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-assistant-board-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import assistant_ui  # noqa: E402
import extension_store  # noqa: E402
import provisioning  # noqa: E402
import provisioning.manager as prov_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────────────────────────────────────────────────────
# _parse_board_json
# ──────────────────────────────────────────────────────────────────────


def test_parse_board_json_shapes() -> bool:
    cases = [
        ("", None, "empty"),
        ("no json here", None, "no object"),
        ('{"a": 1}', {"a": 1}, "plain object"),
        ('prefix {"order": ["x"]} suffix', {"order": ["x"]}, "object with prose"),
        # Greedy first-{ .. last-} span; interspersed prose => unparseable =>
        # None (mirrors session_search._parse_worker_result — the supported
        # shape is a single trailing object, not two objects with text between).
        ('{"a": 1} then {"b": 2}', None, "two objects with prose between"),
        ("[1,2,3]", None, "array is not a dict"),
        ("{bad json}", None, "unparseable"),
    ]
    ok = True
    for text, want, label in cases:
        got = assistant_ui._parse_board_json(text)
        if got != want:
            print(f"{FAIL} parse_board_json[{label}]: got {got!r} want {want!r}")
            ok = False
    if ok:
        print(f"{PASS} _parse_board_json shape handling")
    return ok


# ──────────────────────────────────────────────────────────────────────
# AssistantBoardSpec
# ──────────────────────────────────────────────────────────────────────


def test_board_spec_shape() -> bool:
    spec = assistant_ui.BOARD_SPEC
    ok = True
    if provisioning.get("assistant_board") is not spec:
        print(f"{FAIL} board spec not registered under 'assistant_board'")
        ok = False
    if spec.run_mode != "fork":
        print(f"{FAIL} board spec run_mode={spec.run_mode!r} (want 'fork')")
        ok = False
    if spec.dispatch != "in_process":
        print(f"{FAIL} board spec dispatch={spec.dispatch!r} (want 'in_process')")
        ok = False
    if spec.on_no_fork != "error":
        print(f"{FAIL} board spec on_no_fork={spec.on_no_fork!r} (want 'error')")
        ok = False
    if not spec.bare_config:
        print(f"{FAIL} board spec should be bare_config (no skills)")
        ok = False
    if not spec.machine_completion:
        print(f"{FAIL} board spec should be machine_completion (no tools)")
        ok = False
    if spec.worker_creation_policy != "deny":
        print(f"{FAIL} board spec must deny sub-workers")
        ok = False
    # Per-fork instruction is the identity payload (contract is in the base).
    if spec.build_instructions("hello", {}) != "hello":
        print(f"{FAIL} board spec build_instructions is not identity")
        ok = False
    if ok:
        print(f"{PASS} AssistantBoardSpec shape (fork/in_process/bare/machine)")
    return ok


def test_board_provision_prompt_is_stateless() -> bool:
    """The cached base must NOT carry item state — only the brief + the JSON
    contract (byte-stable across calls → cache-warm)."""
    spec = assistant_ui.BOARD_SPEC
    p1 = spec.build_provision_prompt({})
    p2 = spec.build_provision_prompt({"items": [{"turn_id": "x", "status": "open"}]})
    ok = True
    if p1 != p2:
        print(f"{FAIL} provision prompt varies with ctx (state leaked into base)")
        ok = False
    if "ready" not in p1:
        print(f"{FAIL} provision prompt missing the 'ready' contract")
        ok = False
    if "open|needs_attention|closed" in p1 and False:
        pass  # contract phrasing lives in instructions, not asserted here
    # No state tokens that change per turn should appear in the base.
    for leaked in ("turn_id", "classifications", "<items>", "<finished_turn>"):
        if leaked in p1:
            print(f"{FAIL} provision prompt leaked volatile token {leaked!r}")
            ok = False
    if ok:
        print(f"{PASS} board provision prompt is stateless + cache-stable")
    return ok


# ──────────────────────────────────────────────────────────────────────
# _normalize_classifications
# ──────────────────────────────────────────────────────────────────────


def test_normalize_classifications() -> bool:
    obj = {"classifications": [
        {"turn_id": "a", "status": "open", "summary": "s1"},
        {"id": "b", "status": "closed"},                 # id alias, no summary
        {"status": "open"},                               # no id -> dropped
        {"turn_id": "c"},                                 # no status -> dropped
        "garbage",                                        # non-dict -> dropped
        {"turn_id": "d", "status": "needs_attention", "summary": "blocked"},
    ]}
    out = assistant_ui._normalize_classifications(obj)
    got = [(r["turn_id"], r["status"], r["summary"]) for r in out]
    want = [
        ("a", "open", "s1"),
        ("b", "closed", ""),
        ("d", "needs_attention", "blocked"),
    ]
    if got != want:
        print(f"{FAIL} normalize_classifications: got {got!r} want {want!r}")
        return False
    if assistant_ui._normalize_classifications({"classifications": "nope"}) != []:
        print(f"{FAIL} normalize_classifications: non-list not coerced to []")
        return False
    print(f"{PASS} _normalize_classifications aliasing + bad-row dropping")
    return True


# ──────────────────────────────────────────────────────────────────────
# classify / extract_status / rank with a mocked fork
# ──────────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, value):
        self.value = value


def _patch_fork(monkeypatch_value):
    """Replace provisioning.run with a stub that records the instruction and
    returns a fixed parsed value. Returns (restore_fn, captured)."""
    captured = {}
    original = prov_manager.run

    async def fake_run(spec, query, ctx=None, *, model=None):
        captured["spec"] = spec
        captured["query"] = query
        return _FakeResult(monkeypatch_value)

    # provisioning.run is re-exported; both names point at the same function in
    # assistant_ui's namespace via `provisioning.run`.
    provisioning.run = fake_run  # type: ignore[assignment]
    prov_manager.run = fake_run  # type: ignore[assignment]

    def restore():
        provisioning.run = original  # type: ignore[assignment]
        prov_manager.run = original  # type: ignore[assignment]

    return restore, captured


def _ensure_ext_id() -> bool:
    """The fork helpers no-op when the private registry isn't loaded. Force a
    value so the dispatch path is exercised in the unit test."""
    if extension_store.BUILTIN_ASSISTANT_EXTENSION_ID:
        return True
    extension_store.BUILTIN_ASSISTANT_EXTENSION_ID = "test.assistant"
    return True


def test_classify_builds_instruction_and_normalizes() -> bool:
    _ensure_ext_id()
    restore, captured = _patch_fork({"classifications": [
        {"turn_id": "t1", "status": "closed", "summary": "done"},
    ]})
    try:
        out = _run(assistant_ui.classify([
            {"turn_id": "t1", "user_prompt": "fix", "assistant_message": "shipped"},
        ]))
    finally:
        restore()
    ok = True
    if out != {"classifications": [{"turn_id": "t1", "status": "closed", "summary": "done"}]}:
        print(f"{FAIL} classify output: {out!r}")
        ok = False
    if "<items>" not in captured.get("query", ""):
        print(f"{FAIL} classify instruction missing <items> block")
        ok = False
    if "t1" not in captured.get("query", ""):
        print(f"{FAIL} classify instruction missing the turn_id")
        ok = False
    if ok:
        print(f"{PASS} classify builds tail-block instruction + normalizes")
    return ok


def test_classify_empty_batch_short_circuits() -> bool:
    restore, captured = _patch_fork({"classifications": [{"turn_id": "x", "status": "open"}]})
    try:
        out = _run(assistant_ui.classify([]))
    finally:
        restore()
    if out != {"classifications": []}:
        print(f"{FAIL} classify([]) should be empty, got {out!r}")
        return False
    if captured:
        print(f"{FAIL} classify([]) should not dispatch a fork")
        return False
    print(f"{PASS} classify empty batch short-circuits (no fork)")
    return True


def test_extract_status_builds_turn_and_items() -> bool:
    _ensure_ext_id()
    restore, captured = _patch_fork({"deltas": [
        {"id": "t2", "status": "needs_attention", "summary": "needs spec"},
    ]})
    try:
        out = _run(assistant_ui.extract_status(
            {"source_sid": "s1", "assistant_message": "I need a decision",
             "edited_files": ["a.py"]},
            [{"turn_id": "t2", "user_prompt": "add feat", "status": "open"}],
        ))
    finally:
        restore()
    ok = True
    if out != {"deltas": [
        {"id": "t2", "status": "needs_attention", "summary": "needs spec"}
    ]}:
        print(f"{FAIL} extract_status output: {out!r}")
        ok = False
    q = captured.get("query", "")
    if "<finished_turn>" not in q or "<board_items>" not in q:
        print(f"{FAIL} extract_status instruction missing finished_turn/board_items")
        ok = False
    if "s1" not in q or "t2" not in q:
        print(f"{FAIL} extract_status instruction missing source sid / item id")
        ok = False
    if ok:
        print(f"{PASS} extract_status builds finished-turn + board-items blocks")
    return ok


def test_rank_dedupes_and_appends_missing() -> bool:
    _ensure_ext_id()
    # Fork returns b first, repeats b, drops c, invents z.
    restore, _ = _patch_fork({"order": ["b", "b", "z"]})
    try:
        out = _run(assistant_ui.rank([
            {"turn_id": "a"}, {"turn_id": "b"}, {"turn_id": "c"},
        ]))
    finally:
        restore()
    # b first (fork), then a and c appended in input order (never lost), z dropped.
    if out != {"order": ["b", "a", "c"]}:
        print(f"{FAIL} rank order hygiene: got {out!r} want ['b','a','c']")
        return False
    print(f"{PASS} rank dedupes, drops unknown, appends missing")
    return True


def test_rank_failure_returns_input_order() -> bool:
    _ensure_ext_id()
    restore, _ = _patch_fork({"error": "parse_failed"})  # no 'order' key
    try:
        out = _run(assistant_ui.rank([{"turn_id": "a"}, {"turn_id": "b"}]))
    finally:
        restore()
    if out != {"order": ["a", "b"]}:
        print(f"{FAIL} rank failure should return input order, got {out!r}")
        return False
    print(f"{PASS} rank fork-failure falls back to input order")
    return True


def test_run_board_fork_handles_timeout() -> bool:
    _ensure_ext_id()
    original = prov_manager.run

    async def slow_run(spec, query, ctx=None, *, model=None):
        await asyncio.sleep(5)
        return _FakeResult({})

    provisioning.run = slow_run  # type: ignore[assignment]
    prov_manager.run = slow_run  # type: ignore[assignment]
    try:
        out = _run(assistant_ui._run_board_fork("x", timeout=0.05))
    finally:
        provisioning.run = original  # type: ignore[assignment]
        prov_manager.run = original  # type: ignore[assignment]
    if out != {"error": "timeout"}:
        print(f"{FAIL} _run_board_fork timeout: got {out!r}")
        return False
    print(f"{PASS} _run_board_fork bounds a stuck fork (timeout)")
    return True


# ──────────────────────────────────────────────────────────────────────
# Endpoint body validation (gating proven by the live integration path;
# here we assert the malformed-body rejections that precede any fork).
# ──────────────────────────────────────────────────────────────────────


def test_endpoints_reject_malformed_bodies() -> bool:
    import main  # noqa: E402 — imported late; heavy module
    from fastapi import HTTPException

    # Bypass the auth/runtime gate so we test the body validation that follows.
    orig_gate = main._require_assistant_internal
    main._require_assistant_internal = lambda token: None  # type: ignore[assignment]
    ok = True
    try:
        for coro_factory, label in [
            (lambda: main.internal_assistant_ui_classify(body={"batch": "nope"}, x_internal_token="t"), "classify non-list batch"),
            (lambda: main.internal_assistant_ui_extract_status(body={"target_turn": "nope"}, x_internal_token="t"), "extract-status non-object target"),
            (lambda: main.internal_assistant_ui_rank(body={"items": "nope"}, x_internal_token="t"), "rank non-list items"),
        ]:
            try:
                _run(coro_factory())
                print(f"{FAIL} {label}: expected HTTP 400")
                ok = False
            except HTTPException as exc:
                if exc.status_code != 400:
                    print(f"{FAIL} {label}: status {exc.status_code} (want 400)")
                    ok = False
    finally:
        main._require_assistant_internal = orig_gate  # type: ignore[assignment]
    if ok:
        print(f"{PASS} board endpoints reject malformed bodies (400 before fork)")
    return ok


def main_run() -> int:
    tests = [
        test_parse_board_json_shapes,
        test_board_spec_shape,
        test_board_provision_prompt_is_stateless,
        test_normalize_classifications,
        test_classify_builds_instruction_and_normalizes,
        test_classify_empty_batch_short_circuits,
        test_extract_status_builds_turn_and_items,
        test_rank_dedupes_and_appends_missing,
        test_rank_failure_returns_input_order,
        test_run_board_fork_handles_timeout,
        test_endpoints_reject_malformed_bodies,
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
    print(f"\n{n_pass}/{n_total} assistant board-fork unit tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())

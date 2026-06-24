"""Lock tests for DraftStore — the sidecar persistence sibling
extracted from SessionManager.

Verifies:
  - DraftStore owns dirty/gen state; sm no longer has these fields.
  - set_draft mutates the cached session record via the new public
    `sm.set_draft_inline` (not via the sm internals).
  - The sidecar I/O goes through session_store.write_drafts.
  - DraftStore.is_dirty / note_root_persisted / note_root_dropped
    are reached by sm via on-demand `_draft_store_or_none()`
    resolution (option B — no stored hook refs).
  - Structural lock: sm has no per-root draft coalescer fields,
    methods, or `DRAFT_FLUSH_DELAY` constant.
"""
import ast
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_ds_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_manager import manager as session_manager  # noqa: E402
import session_store  # noqa: E402
from draft_store import DraftStore  # noqa: E402


class _StubCoordinator:
    """Empty stub — DraftStore reaches session_manager (a module
    singleton) directly, not through Coordinator."""


def test_session_manager_has_no_draft_coalescer() -> None:
    """Structural lock: sm must not retain the moved fields/methods
    (dirty/gen dicts, DRAFT_FLUSH_DELAY constant, the 4 internal
    helpers). The thin `set_draft` and `drain_pending_drafts` facades
    are allowed (they delegate to DraftStore)."""
    src = (Path(__file__).resolve().parent.parent / "session_manager.py").read_text()
    tree = ast.parse(src)

    forbidden_attrs = {
        # Moved state.
        "_draft_dirty", "_draft_gen",
        # Hook attrs deleted in the option-B refactor — sm now resolves
        # DraftStore on demand via `_draft_store_or_none()` instead of
        # storing callable refs.
        "_draft_pin_check", "_draft_on_persist", "_draft_on_drop",
    }
    forbidden_methods = {
        "_arm_draft_flush", "_maybe_flush_draft",
        "_flush_draft_sync", "_persist_drafts",
        "_on_flush_done",
        # `bind_draft_hooks` deleted in the option-B refactor.
        "bind_draft_hooks",
    }
    forbidden_constants = {"DRAFT_FLUSH_DELAY"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                # Module-level constants.
                if isinstance(tgt, ast.Name) and tgt.id in forbidden_constants:
                    raise AssertionError(
                        f"session_manager.py still defines {tgt.id} — "
                        "must move to draft_store.py"
                    )
                # Instance attribute assignments self.X.
                if (
                    isinstance(tgt, ast.Attribute)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "self"
                    and tgt.attr in forbidden_attrs
                ):
                    raise AssertionError(
                        f"session_manager.py still assigns self.{tgt.attr} — "
                        "must move to DraftStore"
                    )
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if node.name in forbidden_methods:
                raise AssertionError(
                    f"session_manager.py still defines {node.name} — "
                    "must move to DraftStore"
                )


def test_draft_store_owns_its_own_state() -> None:
    ds = DraftStore(_StubCoordinator())
    assert ds._dirty == set()
    assert ds._gen == {}
    # State lock is its own — not shared with sm.
    assert ds._state_lock is not None


def test_set_draft_routes_through_inline_mutate_and_arms_dirty() -> None:
    """End-to-end (sync path with no loop bound): create a real
    session, call DraftStore.set_draft, verify the in-memory record
    was mutated AND the sidecar was written (since no loop = inline
    flush)."""
    sess = session_manager.create(name="t", model="m", cwd="/tmp")
    sid = sess["id"]
    ds = DraftStore(_StubCoordinator())
    # No bound loop → DraftStore writes the sidecar inline.
    result = ds.set_draft(sid, "typed text", 1)
    assert result is not None
    assert result["draft_input"] == "typed text"
    assert result["draft_input_seq"] == 1
    # Sidecar should exist on disk.
    rid = session_manager._root_id_for(sid)
    drafts = session_store.read_drafts(rid)
    assert sid in drafts
    assert drafts[sid]["draft_input"] == "typed text"


def test_is_dirty_pin_check() -> None:
    """DraftStore.is_dirty must mirror its `_dirty` set. sm wires
    this in as a pin predicate so the root can't be evicted while
    a flush is pending."""
    ds = DraftStore(_StubCoordinator())
    ds._dirty.add("root-1")
    assert ds.is_dirty("root-1") is True
    assert ds.is_dirty("root-other") is False
    ds.note_root_dropped("root-1")
    assert ds.is_dirty("root-1") is False


def test_note_root_persisted_clears_dirty_but_keeps_gen() -> None:
    ds = DraftStore(_StubCoordinator())
    ds._dirty.add("root-1")
    ds._gen["root-1"] = 5
    ds.note_root_persisted("root-1")
    assert "root-1" not in ds._dirty
    # gen is preserved so a stale scheduled flush still self-skips
    # via the gen check.
    assert ds._gen["root-1"] == 5


def test_note_root_dropped_clears_both() -> None:
    ds = DraftStore(_StubCoordinator())
    ds._dirty.add("root-1")
    ds._gen["root-1"] = 5
    ds.note_root_dropped("root-1")
    assert "root-1" not in ds._dirty
    assert "root-1" not in ds._gen


def test_sm_resolves_draft_store_on_demand() -> None:
    """Option B: sm resolves the active DraftStore via
    `_draft_store_or_none()` on every hot-path call rather than
    storing callable refs. Verify:
      - resolver returns None when truly no coordinator (both
        ContextVar and process-default cleared)
      - resolver returns the live store when bound
      - `_is_pinned` sees DraftStore's dirty state through that
        resolution
      - `_is_pinned` fails CLOSED (returns True) when the resolver
        raises (e.g. coord-bound-but-no-draft_store race)
    """
    import orchestrator
    from orchestrator import _active_coordinator_var

    # Save BOTH the ContextVar AND the process-default fallback. The
    # resolver consults `get_active_coordinator()` which checks the
    # ContextVar then falls back to `orchestrator._default_coordinator`
    # — clearing only one leaks the other into the test.
    cv_token = _active_coordinator_var.set(None)
    saved_default = orchestrator._default_coordinator
    orchestrator._default_coordinator = None
    try:
        # No coord → resolver returns None; _is_pinned must not raise
        # AND must not pin (the draft layer has nothing to say).
        assert session_manager._draft_store_or_none() is None

        # Install a stub coord that exposes a DraftStore.
        ds = DraftStore(_StubCoordinator())
        class _CoordWithDS:
            draft_store = ds
        coord = _CoordWithDS()
        _active_coordinator_var.set(coord)

        assert session_manager._draft_store_or_none() is ds
        # Dirty state on the resolved store flows through to sm's pin.
        ds._dirty.add("root-y")
        assert session_manager._is_pinned("root-y", set()) is True
        ds._dirty.discard("root-y")

        # Fail-CLOSED: when the resolver raises (coord bound but no
        # draft_store attr — should never happen in production
        # post-init-ordering-fix, but the test pins the safety net),
        # `_is_pinned` must return True.
        class _CoordMissingDS:
            pass  # no `draft_store` attr
        _active_coordinator_var.set(_CoordMissingDS())
        assert session_manager._is_pinned("root-fail-closed", set()) is True
    finally:
        _active_coordinator_var.reset(cv_token)
        orchestrator._default_coordinator = saved_default


if __name__ == "__main__":
    test_session_manager_has_no_draft_coalescer()
    test_draft_store_owns_its_own_state()
    test_set_draft_routes_through_inline_mutate_and_arms_dirty()
    test_is_dirty_pin_check()
    test_note_root_persisted_clears_dirty_but_keeps_gen()
    test_note_root_dropped_clears_both()
    test_sm_resolves_draft_store_on_demand()
    print("OK: DraftStore — option-B on-demand resolution")

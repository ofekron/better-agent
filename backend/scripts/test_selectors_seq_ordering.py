"""Regression: multi-tab model/provider/reasoning_effort selector changes
must carry a monotonic `selectors_seq` so the frontend can tell "the
session's selector state advanced remotely" (adopt it) apart from "my own
local pick hasn't round-tripped yet" (persist it).

Bug: two tabs/panes open on the same session ping-ponged the model back and
forth forever (105 `model_switched` events in ~30s, strictly alternating
between two models, no live turn running). Root cause: the frontend's
model-drift effect had no way to tell "the session's model just changed
because ANOTHER tab PATCHed it" apart from "the user picked a new model
locally" — both looked identical (local `model` != `currentSession.model`),
so each tab kept PATCHing its now-stale local value back in response to the
other tab's broadcast.

Fix: `SessionManager.set_selectors` bumps a per-session monotonic
`selectors_seq` (via `_bump_selectors_seq`) whenever it actually changes
`model`/`provider_id`/`reasoning_effort` — not on every call, and not for
`cwd`/`runner`/other fields that don't drive the frontend drift decision.
The seq is threaded onto the fired `selectors_set` change dict (via `_run`'s
`enrich` hook) and onto the `session_metadata_updated` WS patch
(`session_ws_broadcaster.py`, see `test_session_ws_broadcaster_dispatch.py`).
The frontend (`frontend/src/utils/modelDrift.ts::resolveModelDriftAction`,
`frontend/tests/modelDrift.test.ts`) adopts instead of patches whenever the
incoming `selectors_seq` is newer than the one it already incorporated.

This test proves the backend half: the seq strictly increases only on a
REAL model/provider_id/reasoning_effort change, is stable across writes that
don't touch those fields (cwd-only, runner-only, resending the same model),
and is present on every `set_selectors` return + fired change dict.

Run with:
    cd backend && .venv/bin/python scripts/test_selectors_seq_ordering.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_selectors_seq_")
os.environ["BETTER_AGENT_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def _run(failures: list[str]) -> None:
    import main

    sm = main.session_manager

    hits: list[tuple[str, dict]] = []
    sm.add_listener(lambda sid, change: hits.append((sid, change)))

    sess = sm.create(name="selectors-seq-test", cwd=_TMP, orchestration_mode="native")
    sid = sess["id"]

    def selector_hits() -> list[dict]:
        return [c for s, c in hits if s == sid and c.get("kind") == "selectors_set"]

    # 0. Freshly created session starts at seq 0 (no selector write yet).
    _check(
        (sm.get(sid) or {}).get("selectors_seq") in (None, 0),
        "fresh session has no selectors_seq bump yet",
        failures,
    )

    # 1. A real model change bumps the seq and returns it from set_selectors.
    updated = sm.set_selectors(sid, model="claude-opus-5[1m]", client_id="tab-A")
    seq_after_a = updated.get("selectors_seq")
    _check(
        isinstance(seq_after_a, int) and seq_after_a >= 1,
        f"model change bumps selectors_seq to a positive int (got {seq_after_a!r})",
        failures,
    )
    _check(
        selector_hits()[-1].get("selectors_seq") == seq_after_a,
        "set_selectors' fired change dict carries the bumped seq",
        failures,
    )

    # 2. THE bug scenario: tab B (unaware of tab A's change) "corrects" the
    #    session back to what it thinks is current. This IS a real model
    #    change (session is now opus, tab B writes sonnet back) so it's
    #    expected to bump again — the backend's job is only to make this
    #    observable/orderable, not to suppress it; suppressing the ping-pong
    #    itself is the frontend's job once it can see the seq moved.
    updated_b = sm.set_selectors(sid, model="claude-sonnet-5", client_id="tab-B")
    seq_after_b = updated_b.get("selectors_seq")
    _check(
        seq_after_b > seq_after_a,
        f"tab B's write strictly increases seq ({seq_after_a!r} -> {seq_after_b!r})",
        failures,
    )

    # 3. Resending the SAME model value must NOT bump the seq — a no-op
    #    write must stay a no-op for ordering purposes (otherwise every
    #    harmless resend would look like a real advance to the frontend).
    noop = sm.set_selectors(sid, model="claude-sonnet-5", client_id="tab-B")
    seq_after_noop = noop.get("selectors_seq")
    _check(
        seq_after_noop == seq_after_b,
        f"resending the current model does not bump seq ({seq_after_b!r} == {seq_after_noop!r})",
        failures,
    )
    _check(
        "selectors_seq" in selector_hits()[-1] and selector_hits()[-1]["selectors_seq"] == seq_after_b,
        "no-op write's fired change dict still carries the (unchanged) seq",
        failures,
    )

    # 4. A cwd-only change must NOT bump the seq — cwd doesn't drive the
    #    frontend drift decision, and bumping on unrelated fields would
    #    make the seq a poor ordering signal specifically for model/
    #    provider_id/reasoning_effort.
    sm.set_selectors(sid, cwd="/tmp/somewhere-else")
    seq_after_cwd = (sm.get(sid) or {}).get("selectors_seq")
    _check(
        seq_after_cwd == seq_after_b,
        f"cwd-only change does not bump selectors_seq ({seq_after_b!r} == {seq_after_cwd!r})",
        failures,
    )

    # 5. provider_id and reasoning_effort changes also bump it.
    from config_store import add_provider

    prov = add_provider({
        "name": "Provider Q", "kind": "claude", "mode": "subscription",
        "nickname": "Q", "default_model": "model-q", "custom_models": ["model-q"],
    })
    sm.set_selectors(sid, provider_id=prov["id"], model="model-q")
    seq_after_provider = (sm.get(sid) or {}).get("selectors_seq")
    _check(
        seq_after_provider > seq_after_cwd,
        f"provider_id change bumps seq ({seq_after_cwd!r} -> {seq_after_provider!r})",
        failures,
    )

    sm.set_selectors(sid, reasoning_effort="high")
    seq_after_effort = (sm.get(sid) or {}).get("selectors_seq")
    _check(
        seq_after_effort > seq_after_provider,
        f"reasoning_effort change bumps seq ({seq_after_provider!r} -> {seq_after_effort!r})",
        failures,
    )

    sm.delete(sid)


def main_entry() -> int:
    failures: list[str] = []
    try:
        _run(failures)
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nselectors_seq ordering check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())

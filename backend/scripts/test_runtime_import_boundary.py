"""Importer-inventory ratchet for the runtime split (plan Phase 0/1).

Backend modules outside the runtime core must reach the coordinator
through `runtime_client.runtime`, never by importing `main`. This scan
fails when a new direct `main` dependency appears; shrink the allowlist
as remaining debt is migrated — never grow it.
"""

import re
import sys
from pathlib import Path

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-runtime-import-boundary-")

BACKEND = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(BACKEND))

# Direct-`main` importers that are allowed to remain:
# - app_entry.py: process entrypoint that boots the FastAPI app.
# - capability_api.py: BFF endpoint dispatch via `getattr(main, fn)`;
#   known debt, becomes a runtime proxy in plan Phase 3.
ALLOWED_MAIN_IMPORTERS = {
    "app_entry.py",
    "capability_api.py",
}

_MAIN_IMPORT = re.compile(
    r"^\s*(from\s+main\s+import\b|import\s+main\b)", re.MULTILINE
)

# `get_active_coordinator` bypasses the typed RuntimeClient surface that
# Phase 3 swaps for IPC. Runtime-core modules may resolve it directly;
# everything else goes through runtime_client. Ratchet: shrink, never grow.
# - runtime_client.py: the canonical facade resolver.
# - orchestrator.py / orchs/, session_manager.py, user_msg_lifecycle.py,
#   user_prompt_manager.py, session_search.py: runtime core.
# - extension_api.py, extension_backend_loader.py, extension_storage_api.py,
#   extension_store.py, provider_config_sync_api.py: ratcheted debt —
#   migrate to runtime_client in a later slice.
ALLOWED_COORDINATOR_RESOLVERS = {
    "runtime_client.py",
    "orchestrator.py",
    "orchs/base.py",
    "session_manager.py",
    "user_msg_lifecycle.py",
    "user_prompt_manager.py",
    "session_search.py",
    "extension_api.py",
    "extension_backend_loader.py",
    "extension_storage_api.py",
    "extension_store.py",
    "provider_config_sync_api.py",
}

_COORDINATOR_RESOLVE = re.compile(r"\bget_active_coordinator\b")


def _scan(pattern: re.Pattern[str]) -> set[str]:
    violations: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith("scripts/"):
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            violations.add(rel)
    return violations


def _main_importers() -> set[str]:
    return _scan(_MAIN_IMPORT)


def test_no_new_direct_main_importers():
    found = _main_importers()
    unexpected = sorted(found - ALLOWED_MAIN_IMPORTERS)
    assert not unexpected, (
        "new direct `main` importers detected; use runtime_client.runtime "
        f"instead: {unexpected}"
    )


def test_allowlist_has_no_stale_entries():
    found = _main_importers()
    stale = sorted(ALLOWED_MAIN_IMPORTERS - found)
    assert not stale, (
        f"allowlist entries no longer import main — ratchet them out: {stale}"
    )


def test_no_new_direct_coordinator_resolvers():
    found = _scan(_COORDINATOR_RESOLVE)
    unexpected = sorted(found - ALLOWED_COORDINATOR_RESOLVERS)
    assert not unexpected, (
        "new direct get_active_coordinator users detected; use "
        f"runtime_client.runtime instead: {unexpected}"
    )


def test_coordinator_resolver_allowlist_has_no_stale_entries():
    found = _scan(_COORDINATOR_RESOLVE)
    stale = sorted(ALLOWED_COORDINATOR_RESOLVERS - found)
    assert not stale, (
        "allowlist entries no longer resolve the coordinator directly — "
        f"ratchet them out: {stale}"
    )


def test_runtime_client_fails_closed_without_runtime():
    import contextvars

    import runtime_client
    from orchestrator import _active_coordinator_var

    def _probe() -> None:
        assert _active_coordinator_var.get() is None
        try:
            runtime_client.runtime.in_flight_assistant_msg("nope")
        except runtime_client.RuntimeUnavailableError:
            return
        raise AssertionError("expected RuntimeUnavailableError")

    # Fresh context: no per-task coordinator; rely on no default being
    # registered in this test process.
    import orchestrator

    saved_default = orchestrator._default_coordinator
    orchestrator._default_coordinator = None
    try:
        contextvars.copy_context().run(_probe)
    finally:
        orchestrator._default_coordinator = saved_default


def test_facade_forwards_to_bound_fake_coordinator():
    import asyncio

    import _fake_runtime
    from runtime_client import runtime

    calls: list[tuple] = []

    class Fake:
        turn_manager = None

        async def _dispatch_messages_delta(
            self, root_id: str, sid: str, msg: dict, *,
            omit_render_events: bool = False,
        ):
            calls.append((root_id, sid, msg["id"], omit_render_events))

    async def _drive() -> None:
        with _fake_runtime.bind_coordinator(Fake()):
            await runtime.dispatch_messages_delta("r", "r", {"id": "m1"})
            await runtime.dispatch_messages_delta(
                "r", "r", {"id": "m2"}, omit_render_events=True,
            )

    asyncio.run(_drive())
    assert calls == [("r", "r", "m1", False), ("r", "r", "m2", True)]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)

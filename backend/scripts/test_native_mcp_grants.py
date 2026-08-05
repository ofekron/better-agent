"""Unit tests for native_mcp_grants.py — the scoped grant store + resolver
for extension-declared native MCP servers.

Covers:
  * digest binding — a grant only resolves while the extension's current
    declaration still hashes to what the grant was created against
    (security property; a manifest update invalidates outstanding grants).
  * scope matching for global/project — the two scopes this store's grants
    are actually reachable through in PR1.
  * qualified-name namespacing — no cross-extension/user collision by
    construction.
  * lifecycle helpers (remove_grants_for_extension/target) are exact,
    don't touch unrelated grants.
  * a disabled/uninstalled extension's grants resolve to nothing (absent
    from active_declarations), with no separate check needed.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_test_home.isolate("bc-test-native-mcp-grants-")

import native_mcp_grants as g  # noqa: E402


def _decl(command="python3", args=("server.py",), env_keys=(), scopes=("global", "project"), package_fingerprint="fp-1"):
    return g.ServerDeclaration(command=command, args=args, env_keys=env_keys, scopes=scopes, package_fingerprint=package_fingerprint)


def test_global_grant_resolves_everywhere():
    decl = _decl()
    g.add_grant(
        extension_id="ext-a", server_id="cards", scope="global", target="",
        digest=decl.digest(), created_at="2024-01-01T00:00:00Z",
    )
    resolved = g.resolve_native_mcp_servers(
        active_declarations={("ext-a", "cards"): decl},
        project_path="/tmp/somewhere", session_id="s1", root_id="r1", turn_id="t1",
    )
    assert "ext-a:cards" in resolved
    assert resolved["ext-a:cards"]["command"] == "python3"
    g.remove_grant(extension_id="ext-a", server_id="cards", scope="global", target="")


def test_digest_mismatch_fails_closed():
    original = _decl(args=("server.py",))
    g.add_grant(
        extension_id="ext-b", server_id="cards", scope="global", target="",
        digest=original.digest(), created_at="2024-01-01T00:00:00Z",
    )
    updated = _decl(args=("server.py", "--danger"))  # extension shipped a new declaration
    resolved = g.resolve_native_mcp_servers(active_declarations={("ext-b", "cards"): updated})
    assert "ext-b:cards" not in resolved
    g.remove_grant(extension_id="ext-b", server_id="cards", scope="global", target="")


def test_digest_ignores_interpreter_path_but_binds_everything_else():
    # The interpreter path is whichever platform process computes the
    # declaration (backend dependency-plan venv vs. ambient launcher .venv);
    # binding it made a grant created in one context silently unresolvable in
    # the other. A grant created under one interpreter MUST resolve under
    # another, while every extension-controlled field stays binding.
    created = _decl(command="/backend/.venvs/abc/bin/python")
    g.add_grant(
        extension_id="ext-i", server_id="cards", scope="global", target="",
        digest=created.digest(), created_at="2024-01-01T00:00:00Z",
    )
    other_interpreter = _decl(command="/backend/.venv/bin/python")
    resolved = g.resolve_native_mcp_servers(active_declarations={("ext-i", "cards"): other_interpreter})
    assert "ext-i:cards" in resolved, f"grant did not resolve across interpreters: {list(resolved)}"
    # Every extension-controlled field stays binding.
    for changed in (
        _decl(args=("server.py", "--extra")),
        _decl(env_keys=("A_KEY",)),
        _decl(scopes=("global",)),
        _decl(package_fingerprint="fp-2"),
    ):
        assert created.digest() != changed.digest()
    g.remove_grant(extension_id="ext-i", server_id="cards", scope="global", target="")


def test_package_fingerprint_change_invalidates_grant_even_with_identical_launcher():
    # command/args are always the fixed launcher stub (extension_id, item_name)
    # -- they never change between an extension's own versions. Without a
    # content fingerprint in the digest, an update that rewrites the
    # extension's actual server.py behavior would produce an IDENTICAL
    # digest and the grant would silently keep resolving (BL4). This is
    # sourced from a real installed-package content hash
    # (extension_store._runtime_package_fingerprint), not a self-declared
    # version string the same party could leave stale.
    original = _decl(package_fingerprint="fp-abc123")
    g.add_grant(
        extension_id="ext-v", server_id="cards", scope="global", target="",
        digest=original.digest(), created_at="2024-01-01T00:00:00Z",
    )
    updated = _decl(package_fingerprint="fp-def456")  # same command/args/scopes, files on disk changed
    resolved = g.resolve_native_mcp_servers(active_declarations={("ext-v", "cards"): updated})
    assert "ext-v:cards" not in resolved
    g.remove_grant(extension_id="ext-v", server_id="cards", scope="global", target="")


def test_project_scope_matches_only_its_own_project():
    decl = _decl()
    target = g.project_target("primary", "/tmp/project-a")
    g.add_grant(
        extension_id="ext-c", server_id="cards", scope="project", target=target,
        digest=decl.digest(), created_at="2024-01-01T00:00:00Z",
    )
    in_project = g.resolve_native_mcp_servers(
        active_declarations={("ext-c", "cards"): decl},
        node_id="primary", project_path="/tmp/project-a",
    )
    other_project = g.resolve_native_mcp_servers(
        active_declarations={("ext-c", "cards"): decl},
        node_id="primary", project_path="/tmp/project-b",
    )
    no_project = g.resolve_native_mcp_servers(active_declarations={("ext-c", "cards"): decl})
    assert "ext-c:cards" in in_project
    assert "ext-c:cards" not in other_project
    assert "ext-c:cards" not in no_project
    g.remove_grant(extension_id="ext-c", server_id="cards", scope="project", target=target)


def test_disabled_extension_grant_resolves_to_nothing():
    decl = _decl()
    g.add_grant(
        extension_id="ext-d", server_id="cards", scope="global", target="",
        digest=decl.digest(), created_at="2024-01-01T00:00:00Z",
    )
    # Caller (extension_store) simply omits a disabled extension's
    # declarations from active_declarations -- no special-case needed here.
    resolved = g.resolve_native_mcp_servers(active_declarations={})
    assert resolved == {}
    g.remove_grant(extension_id="ext-d", server_id="cards", scope="global", target="")


def test_qualified_names_cannot_collide():
    decl_x = _decl(command="cmd-x")
    decl_y = _decl(command="cmd-y")
    g.add_grant(extension_id="ext-x", server_id="cards", scope="global", target="", digest=decl_x.digest(), created_at="t")
    g.add_grant(extension_id="ext-y", server_id="cards", scope="global", target="", digest=decl_y.digest(), created_at="t")
    resolved = g.resolve_native_mcp_servers(
        active_declarations={("ext-x", "cards"): decl_x, ("ext-y", "cards"): decl_y},
    )
    assert resolved.get("ext-x:cards", {}).get("command") == "cmd-x"
    assert resolved.get("ext-y:cards", {}).get("command") == "cmd-y"
    g.remove_grant(extension_id="ext-x", server_id="cards", scope="global", target="")
    g.remove_grant(extension_id="ext-y", server_id="cards", scope="global", target="")


def test_remove_grants_for_extension_only_touches_that_extension():
    decl = _decl()
    g.add_grant(extension_id="ext-p", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-p", server_id="b", scope="global", target="", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-q", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    removed = g.remove_grants_for_extension("ext-p")
    remaining_p = g.list_grants(extension_id="ext-p")
    remaining_q = g.list_grants(extension_id="ext-q")
    assert removed == 2
    assert remaining_p == []
    assert len(remaining_q) == 1
    g.remove_grants_for_extension("ext-q")


def test_remove_grants_for_target_only_touches_that_scope_and_target():
    decl = _decl()
    g.add_grant(extension_id="ext-r", server_id="a", scope="session", target="sess-1", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-r", server_id="b", scope="session", target="sess-2", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-r", server_id="c", scope="global", target="", digest=decl.digest(), created_at="t")
    removed = g.remove_grants_for_target("session", "sess-1")
    remaining = g.list_grants(extension_id="ext-r")
    assert removed == 1
    assert {gr.server_id for gr in remaining} == {"b", "c"}
    g.remove_grants_for_extension("ext-r")


def test_add_grant_is_idempotent_on_same_key():
    decl = _decl()
    g.add_grant(extension_id="ext-s", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t1")
    g.add_grant(extension_id="ext-s", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t2")
    grants = g.list_grants(extension_id="ext-s")
    assert len(grants) == 1
    assert grants[0].created_at == "t2"
    g.remove_grants_for_extension("ext-s")


def test_schema_version_mismatch_is_treated_as_empty_not_raised():
    # Deliberate deviation from this repo's general "raise on unexpected
    # shape, no migrations" convention: a corrupted/foreign grants file
    # fails closed to EMPTY (no grants resolve) rather than raising and
    # taking down the whole extension-reconcile path over an optional
    # enhancement-layer store. Locking the actual behavior AND that the
    # degradation is loud (logged), not a silent "no grants".
    import logging
    from json_store import write_json_durable

    decl = _decl()
    g.add_grant(extension_id="ext-w", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    write_json_durable(g._store_path(), {
        "schema_version": g.SCHEMA_VERSION + 999,
        "grants": [{"extension_id": "ext-w", "server_id": "a", "scope": "global", "target": "", "digest": decl.digest(), "created_at": "t"}],
    })

    class _CaptureHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    capture = _CaptureHandler()
    g.logger.addHandler(capture)
    g.logger.setLevel(logging.WARNING)
    try:
        grants = g.list_grants()
    finally:
        g.logger.removeHandler(capture)
    warned = any("schema_version mismatch" in r.getMessage() for r in capture.records)
    assert grants == []
    assert warned, "an unrecognized schema_version must log a warning, not pass silently"
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})


def test_concurrent_add_grant_does_not_lose_a_grant():
    import threading
    errors: list[BaseException] = []

    def add(i: int) -> None:
        try:
            decl = _decl()
            g.add_grant(
                extension_id="ext-conc", server_id=f"srv-{i}", scope="global", target="",
                digest=decl.digest(), created_at="t",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    grants = g.list_grants(extension_id="ext-conc")
    assert not errors, f"concurrent add_grant raised: {errors}"
    assert len(grants) == 16, f"lost a grant under concurrency: {len(grants)}/16"
    g.remove_grants_for_extension("ext-conc")


def test_digest_is_full_length_not_truncated():
    # M3: was truncated to 16 hex chars (64 bits) -- not a comfortable
    # margin for a value whose entire purpose is collision resistance on a
    # security gate, and there was no reason to truncate a value that only
    # lives in a JSON file.
    decl = _decl()
    assert len(decl.digest()) == 64  # full sha256 hexdigest


def test_forged_target_via_separator_injection_is_rejected():
    # M2: _TARGET_SEP-containing components must fail closed rather than
    # silently producing a target string that could collide with a
    # different node's grant.
    assert g.project_target("primary", f"/tmp/x{g._TARGET_SEP}evil") is None
    assert g.project_target(f"evil{g._TARGET_SEP}node", "/tmp/x") is None
    assert g.turn_target(f"root{g._TARGET_SEP}x", "turn-1") is None
    assert g.turn_target("root-1", f"turn{g._TARGET_SEP}x") is None


def test_add_grant_enforces_scope_target_invariants():
    # M4: add_grant is this store's public API, not just
    # extension_store.grant_native_mcp_server's implementation detail --
    # enforce the scope<->target shape the resolver assumes at the write
    # boundary itself.
    decl = _decl()
    cases_should_reject = [
        ("global", "not-the-empty-sentinel"),
        ("project", ""),
        ("project", "no-separator-here"),
        ("turn", ""),
        ("session", ""),
    ]
    for scope, target in cases_should_reject:
        with pytest.raises(ValueError):
            g.add_grant(extension_id="ext-inv", server_id="a", scope=scope, target=target, digest=decl.digest(), created_at="t")
    g.remove_grants_for_extension("ext-inv")


def test_unparseable_grant_row_is_logged_not_silently_dropped():
    # M1: a malformed row is destroyed by the next unrelated write with no
    # signal unless the drop is logged.
    import logging
    from json_store import write_json_durable

    class _CaptureHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    write_json_durable(g._store_path(), {
        "schema_version": g.SCHEMA_VERSION,
        "grants": [{"extension_id": "ext-mal", "server_id": "a", "scope": "not-a-real-scope", "target": "", "digest": "d", "created_at": "t"}],
    })
    capture = _CaptureHandler()
    g.logger.addHandler(capture)
    g.logger.setLevel(logging.WARNING)
    try:
        grants = g.list_grants()
    finally:
        g.logger.removeHandler(capture)
    warned = any("dropping" in r.getMessage() for r in capture.records)
    assert grants == []
    assert warned, "an unparseable grant row must be logged, not silently dropped"
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})

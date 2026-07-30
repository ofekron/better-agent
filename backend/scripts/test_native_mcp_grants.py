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

Run with:
    cd backend && .venv/bin/python scripts/test_native_mcp_grants.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-mcp-grants-")

import native_mcp_grants as g  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _decl(command="python3", args=("server.py",), env_keys=(), scopes=("global", "project"), package_fingerprint="fp-1"):
    return g.ServerDeclaration(command=command, args=args, env_keys=env_keys, scopes=scopes, package_fingerprint=package_fingerprint)


def test_global_grant_resolves_everywhere() -> bool:
    decl = _decl()
    grant = g.add_grant(
        extension_id="ext-a", server_id="cards", scope="global", target="",
        digest=decl.digest(), created_at="2024-01-01T00:00:00Z",
    )
    resolved = g.resolve_native_mcp_servers(
        active_declarations={("ext-a", "cards"): decl},
        project_path="/tmp/somewhere", session_id="s1", root_id="r1", turn_id="t1",
    )
    ok = "ext-a:cards" in resolved and resolved["ext-a:cards"]["command"] == "python3"
    print(f"{OK if ok else FAIL} global grant resolves regardless of project/session/turn context (got {list(resolved)})")
    g.remove_grant(extension_id="ext-a", server_id="cards", scope="global", target="")
    return ok


def test_digest_mismatch_fails_closed() -> bool:
    original = _decl(args=("server.py",))
    g.add_grant(
        extension_id="ext-b", server_id="cards", scope="global", target="",
        digest=original.digest(), created_at="2024-01-01T00:00:00Z",
    )
    updated = _decl(args=("server.py", "--danger"))  # extension shipped a new declaration
    resolved = g.resolve_native_mcp_servers(active_declarations={("ext-b", "cards"): updated})
    ok = "ext-b:cards" not in resolved
    print(f"{OK if ok else FAIL} stale grant does not resolve once the manifest declaration changes (got {list(resolved)})")
    g.remove_grant(extension_id="ext-b", server_id="cards", scope="global", target="")
    return ok


def test_digest_ignores_interpreter_path_but_binds_everything_else() -> bool:
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
    across_interpreters = "ext-i:cards" in resolved
    still_binding = all(
        created.digest() != changed.digest()
        for changed in (
            _decl(args=("server.py", "--extra")),
            _decl(env_keys=("A_KEY",)),
            _decl(scopes=("global",)),
            _decl(package_fingerprint="fp-2"),
        )
    )
    ok = across_interpreters and still_binding
    print(f"{OK if ok else FAIL} digest ignores the interpreter path but binds args/env/scopes/fingerprint "
          f"(across_interpreters={across_interpreters}, still_binding={still_binding})")
    g.remove_grant(extension_id="ext-i", server_id="cards", scope="global", target="")
    return ok


def test_package_fingerprint_change_invalidates_grant_even_with_identical_launcher() -> bool:
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
    ok = "ext-v:cards" not in resolved
    print(f"{OK if ok else FAIL} a package content fingerprint change invalidates the grant even though the launcher command/args are unchanged (got {list(resolved)})")
    g.remove_grant(extension_id="ext-v", server_id="cards", scope="global", target="")
    return ok


def test_project_scope_matches_only_its_own_project() -> bool:
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
    ok = (
        "ext-c:cards" in in_project
        and "ext-c:cards" not in other_project
        and "ext-c:cards" not in no_project
    )
    print(f"{OK if ok else FAIL} project grant matches only its own project "
          f"(in={list(in_project)}, other={list(other_project)}, none={list(no_project)})")
    g.remove_grant(extension_id="ext-c", server_id="cards", scope="project", target=target)
    return ok


def test_disabled_extension_grant_resolves_to_nothing() -> bool:
    decl = _decl()
    g.add_grant(
        extension_id="ext-d", server_id="cards", scope="global", target="",
        digest=decl.digest(), created_at="2024-01-01T00:00:00Z",
    )
    # Caller (extension_store) simply omits a disabled extension's
    # declarations from active_declarations -- no special-case needed here.
    resolved = g.resolve_native_mcp_servers(active_declarations={})
    ok = resolved == {}
    print(f"{OK if ok else FAIL} extension absent from active_declarations contributes nothing (got {resolved})")
    g.remove_grant(extension_id="ext-d", server_id="cards", scope="global", target="")
    return ok


def test_qualified_names_cannot_collide() -> bool:
    decl_x = _decl(command="cmd-x")
    decl_y = _decl(command="cmd-y")
    g.add_grant(extension_id="ext-x", server_id="cards", scope="global", target="", digest=decl_x.digest(), created_at="t")
    g.add_grant(extension_id="ext-y", server_id="cards", scope="global", target="", digest=decl_y.digest(), created_at="t")
    resolved = g.resolve_native_mcp_servers(
        active_declarations={("ext-x", "cards"): decl_x, ("ext-y", "cards"): decl_y},
    )
    ok = (
        resolved.get("ext-x:cards", {}).get("command") == "cmd-x"
        and resolved.get("ext-y:cards", {}).get("command") == "cmd-y"
    )
    print(f"{OK if ok else FAIL} same short server_id from two extensions never collides (got {resolved})")
    g.remove_grant(extension_id="ext-x", server_id="cards", scope="global", target="")
    g.remove_grant(extension_id="ext-y", server_id="cards", scope="global", target="")
    return ok


def test_remove_grants_for_extension_only_touches_that_extension() -> bool:
    decl = _decl()
    g.add_grant(extension_id="ext-p", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-p", server_id="b", scope="global", target="", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-q", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    removed = g.remove_grants_for_extension("ext-p")
    remaining_p = g.list_grants(extension_id="ext-p")
    remaining_q = g.list_grants(extension_id="ext-q")
    ok = removed == 2 and remaining_p == [] and len(remaining_q) == 1
    print(f"{OK if ok else FAIL} remove_grants_for_extension removes exactly that extension's grants "
          f"(removed={removed}, p_left={len(remaining_p)}, q_left={len(remaining_q)})")
    g.remove_grants_for_extension("ext-q")
    return ok


def test_remove_grants_for_target_only_touches_that_scope_and_target() -> bool:
    decl = _decl()
    g.add_grant(extension_id="ext-r", server_id="a", scope="session", target="sess-1", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-r", server_id="b", scope="session", target="sess-2", digest=decl.digest(), created_at="t")
    g.add_grant(extension_id="ext-r", server_id="c", scope="global", target="", digest=decl.digest(), created_at="t")
    removed = g.remove_grants_for_target("session", "sess-1")
    remaining = g.list_grants(extension_id="ext-r")
    ok = removed == 1 and {gr.server_id for gr in remaining} == {"b", "c"}
    print(f"{OK if ok else FAIL} remove_grants_for_target only removes the matching scope+target "
          f"(removed={removed}, remaining_ids={[gr.server_id for gr in remaining]})")
    g.remove_grants_for_extension("ext-r")
    return ok


def test_add_grant_is_idempotent_on_same_key() -> bool:
    decl = _decl()
    g.add_grant(extension_id="ext-s", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t1")
    g.add_grant(extension_id="ext-s", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t2")
    grants = g.list_grants(extension_id="ext-s")
    ok = len(grants) == 1 and grants[0].created_at == "t2"
    print(f"{OK if ok else FAIL} re-adding the same (ext, server, scope, target) key replaces, not duplicates (got {grants})")
    g.remove_grants_for_extension("ext-s")
    return ok


def test_schema_version_mismatch_is_treated_as_empty_not_raised() -> bool:
    # Deliberate deviation from this repo's general "raise on unexpected
    # shape, no migrations" convention: a corrupted/foreign grants file
    # fails closed to EMPTY (no grants resolve) rather than raising and
    # taking down the whole extension-reconcile path over an optional
    # enhancement-layer store. Locking the actual behavior, not the
    # aspirational one -- AND that the degradation is loud (logged), not a
    # silent "no grants" indistinguishable from "nothing was ever granted".
    import logging
    decl = _decl()
    g.add_grant(extension_id="ext-w", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    from json_store import write_json_durable
    write_json_durable(g._store_path(), {"schema_version": 99, "grants": [{"extension_id": "ext-w", "server_id": "a", "scope": "global", "target": "", "digest": decl.digest(), "created_at": "t"}]})

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
    ok = grants == [] and warned
    print(f"{OK if ok else FAIL} an unrecognized schema_version is treated as an empty store AND logs a warning, not raised silently "
          f"(got grants={grants}, warned={warned})")
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})
    return ok


def test_concurrent_add_grant_does_not_lose_a_grant() -> bool:
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
    ok = not errors and len(grants) == 16
    print(f"{OK if ok else FAIL} 16 concurrent add_grant calls to distinct keys all persist, none lost "
          f"(errors={errors}, count={len(grants)})")
    g.remove_grants_for_extension("ext-conc")
    return ok


def test_digest_is_full_length_not_truncated() -> bool:
    # M3: was truncated to 16 hex chars (64 bits) -- not a comfortable
    # margin for a value whose entire purpose is collision resistance on a
    # security gate, and there was no reason to truncate a value that only
    # lives in a JSON file.
    decl = _decl()
    ok = len(decl.digest()) == 64  # full sha256 hexdigest
    print(f"{OK if ok else FAIL} digest() returns the full sha256 hexdigest, not truncated (len={len(decl.digest())})")
    return ok


def test_forged_target_via_separator_injection_is_rejected() -> bool:
    # M2: _TARGET_SEP-containing components must fail closed rather than
    # silently producing a target string that could collide with a
    # different node's grant.
    ok = (
        g.project_target("primary", f"/tmp/x{g._TARGET_SEP}evil") is None
        and g.project_target(f"evil{g._TARGET_SEP}node", "/tmp/x") is None
        and g.turn_target(f"root{g._TARGET_SEP}x", "turn-1") is None
        and g.turn_target("root-1", f"turn{g._TARGET_SEP}x") is None
    )
    print(f"{OK if ok else FAIL} project_target/turn_target fail closed when a component contains _TARGET_SEP")
    return ok


def test_add_grant_enforces_scope_target_invariants() -> bool:
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
    ok = True
    for scope, target in cases_should_reject:
        try:
            g.add_grant(extension_id="ext-inv", server_id="a", scope=scope, target=target, digest=decl.digest(), created_at="t")
            ok = False
            print(f"{FAIL} add_grant accepted an invalid scope/target combo: {scope!r}/{target!r}")
        except ValueError:
            pass
    print(f"{OK if ok else FAIL} add_grant rejects invalid scope/target combinations")
    g.remove_grants_for_extension("ext-inv")
    return ok


def test_unparseable_grant_row_is_logged_not_silently_dropped() -> bool:
    # M1: a malformed row is destroyed by the next unrelated write with no
    # signal unless the drop is logged.
    import logging

    class _CaptureHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    from json_store import write_json_durable
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
    ok = grants == [] and warned
    print(f"{OK if ok else FAIL} an unparseable grant row is dropped AND logged, not silently lost (got grants={grants}, warned={warned})")
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})
    return ok


def main_run() -> int:
    tests = [
        test_global_grant_resolves_everywhere,
        test_digest_mismatch_fails_closed,
        test_digest_ignores_interpreter_path_but_binds_everything_else,
        test_package_fingerprint_change_invalidates_grant_even_with_identical_launcher,
        test_project_scope_matches_only_its_own_project,
        test_disabled_extension_grant_resolves_to_nothing,
        test_qualified_names_cannot_collide,
        test_remove_grants_for_extension_only_touches_that_extension,
        test_remove_grants_for_target_only_touches_that_scope_and_target,
        test_add_grant_is_idempotent_on_same_key,
        test_schema_version_mismatch_is_treated_as_empty_not_raised,
        test_concurrent_add_grant_does_not_lose_a_grant,
        test_digest_is_full_length_not_truncated,
        test_forged_target_via_separator_injection_is_rejected,
        test_add_grant_enforces_scope_target_invariants,
        test_unparseable_grant_row_is_logged_not_silently_dropped,
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
    print(f"\n{n_pass}/{n_total} native_mcp_grants tests passed")
    import shutil
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())

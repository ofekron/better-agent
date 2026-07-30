"""Integration tests for the extension_store <-> native_mcp_grants wiring —
grant_native_mcp_server/revoke_native_mcp_server, the 3 real resolution call
sites (native delivery configs, resolve_native_mcp_server_config,
extension_harness_additions), uninstall/disable lifecycle, and digest
invalidation across an actual installed-extension record (not the isolated
unit tests in test_native_mcp_grants.py, which never touch extension_store).

Run with:
    cd backend && .venv/bin/python scripts/test_native_mcp_grant_wiring.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-mcp-grant-wiring-")
_TMP_OS_HOME = tempfile.mkdtemp(prefix="bc-test-native-mcp-grant-wiring-os-home-")
os.environ["HOME"] = _TMP_OS_HOME

import extension_store  # noqa: E402
import native_mcp_grants  # noqa: E402
import installation_profile  # noqa: E402

# Bare test homes have no installation.json -- _record_active() gates on
# installation_profile.integrations_enabled(), which is False without one.
# Same monkeypatch test_cross_provider_harness_parity.py already uses.
installation_profile.integrations_enabled = lambda: True

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

EXT_ID = "ofek.native-mcp-fixture"
SERVER_ID = "cards"


def _record(*, args=("mcp/server.py",), scopes=("global", "project")) -> Path:
    package = Path(tempfile.mkdtemp(prefix="bc-test-native-mcp-fixture-")) / "native-mcp-fixture"
    (package / "mcp").mkdir(parents=True)
    manifest = extension_store.validate_manifest({
        "kind": "better-agent-extension",
        "id": EXT_ID,
        "name": "Native MCP Fixture",
        "version": "1.0.0",
        "description": "Fixture for grant wiring tests.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": SERVER_ID,
                    "python": "mcp/server.py",
                    "user_facing": False,
                    "bare_allowed": True,
                    "requires_backend_auth": False,
                    "ambient_native": True,
                }
            ],
        },
        "permissions": {"native_mcp": {SERVER_ID: list(scopes)}} if scopes else {},
        "marketplace": {},
    })
    (package / "better-agent-extension.json").write_text(
        __import__("json").dumps(manifest), encoding="utf-8"
    )
    (package / "mcp" / "server.py").write_text("print('cards mcp')\n", encoding="utf-8")
    data = extension_store._load()
    data["extensions"][EXT_ID] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "test",
        "updated_at": "test",
        "source": {
            "type": "test-recorded-runtime",
            "repo_url": "",
            "extension_path": "native-mcp-fixture",
            "ref": "",
            "commit_sha": "native-mcp-fixture-test",
            "install_path": str(package),
        },
        "entitlement": {"status": "active"},
    }
    extension_store._save(data, resurrect_extension_ids={EXT_ID})
    return package


def _cleanup() -> None:
    native_mcp_grants.remove_grants_for_extension(EXT_ID)
    data = extension_store._load()
    data["extensions"].pop(EXT_ID, None)
    extension_store._save(data)


def test_permission_declaration_round_trips_through_manifest_validation() -> bool:
    _record()
    record = extension_store.get_extension(EXT_ID)
    ok = record is not None and record["manifest"]["permissions"]["native_mcp"] == {SERVER_ID: ["global", "project"]}
    print(f"{OK if ok else FAIL} permissions.native_mcp round-trips through manifest validation")
    _cleanup()
    return ok


def test_symlink_escaping_install_root_fails_closed() -> bool:
    # A symlink inside the package pointing outside install_root must not
    # silently escape the hash (content never seen -> incomplete fingerprint)
    # or silently escape the boundary (hashing arbitrary filesystem content
    # outside the package). Fail closed: no fingerprint, no declarations,
    # the extension's native MCP servers simply don't resolve until fixed.
    package = _record()
    outside = Path(tempfile.mkdtemp(prefix="bc-test-outside-install-root-"))
    (outside / "secret.txt").write_text("outside install_root", encoding="utf-8")
    (package / "escape-link").symlink_to(outside / "secret.txt")
    fp = extension_store._native_mcp_package_content_fingerprint(extension_store.get_extension(EXT_ID))
    ok = fp is None
    print(f"{OK if ok else FAIL} a symlink escaping install_root fails the fingerprint closed (got {fp!r})")
    _cleanup()
    return ok


def test_grant_global_makes_server_resolve_in_native_delivery_configs() -> bool:
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    configs = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID in configs
    print(f"{OK if ok else FAIL} global grant makes server appear in native_mcp_server_configs (got {list(configs)})")
    _cleanup()
    return ok


def test_no_grant_omits_server_from_native_delivery_configs() -> bool:
    _record()
    configs = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID not in configs
    print(f"{OK if ok else FAIL} no grant -> server absent from native_mcp_server_configs (got {list(configs)})")
    _cleanup()
    return ok


def test_bytecode_cache_churn_does_not_false_invalidate_grant() -> bool:
    # Real bug caught while building the whole-package content fingerprint:
    # importing/compiling the extension's own server.py between grant time
    # and resolve time writes __pycache__/*.pyc under install_path, which a
    # naive whole-directory hash would pick up as a content change and
    # false-invalidate every grant on the very next resolve. Simulates the
    # bytecode write directly (no real import needed) to lock the exclusion.
    # A standalone .pyc WITH a .py sibling is regenerable bytecode too (same
    # rule, outside __pycache__).
    package = _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    before = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    pycache = package / "mcp" / "__pycache__"
    pycache.mkdir(parents=True, exist_ok=True)
    (pycache / "server.cpython-000.pyc").write_bytes(b"\x00fake bytecode\x00")
    (package / "mcp" / "server.pyc").write_bytes(b"\x00co-located regenerable bytecode\x00")
    after = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID in before and SERVER_ID in after
    print(f"{OK if ok else FAIL} __pycache__ and .py-covered .pyc churn under install_path does not false-invalidate the grant "
          f"(before={list(before)}, after={list(after)})")
    _cleanup()
    return ok


def test_venv_dependency_change_does_invalidate_grant() -> bool:
    # Corrected boundary: vendored dependencies in .venv run in the same
    # process with the same privileges as the extension's own code, so a
    # change under .venv MUST invalidate the grant -- unlike __pycache__,
    # this is not regenerable/reproducible from source, it's a real content
    # change that can alter what the server actually does at runtime.
    package = _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    before = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    venv_pkg = package / ".venv" / "some-dependency"
    venv_pkg.mkdir(parents=True, exist_ok=True)
    (venv_pkg / "__init__.py").write_text("print('a swapped dependency')\n", encoding="utf-8")
    after = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID in before and SERVER_ID not in after
    print(f"{OK if ok else FAIL} a change under .venv invalidates the grant (before={list(before)}, after={list(after)})")
    _cleanup()
    return ok


def test_standalone_pyc_without_py_sibling_is_hashed() -> bool:
    # A package shipping compiled-only modules (no .py sibling) has no
    # regenerable source elsewhere -- that .pyc IS the source of record and
    # must be hashed like any other file, not blanket-excluded by suffix.
    package = _record()
    (package / "mcp" / "compiled_only.pyc").write_bytes(b"\x00v1\x00")
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    before = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    (package / "mcp" / "compiled_only.pyc").write_bytes(b"\x00v2 -- different content\x00")
    after = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID in before and SERVER_ID not in after
    print(f"{OK if ok else FAIL} a standalone .pyc with no .py sibling is hashed, not blanket-excluded "
          f"(before={list(before)}, after={list(after)})")
    _cleanup()
    return ok


def test_grant_gate_admits_identical_server_set_across_provider_wrappers() -> bool:
    # native_mcp_server_configs() is Claude's (runner.py) call; Codex and
    # Gemini both go through builtin_mcp_config.with_builtin_mcp_servers ->
    # native_mcp_launcher_server_configs(). Both route through the SAME
    # _mcp_server_configs_for_delivery(delivery=_HARNESS_DELIVERY_NATIVE, ...)
    # grant gate -- launcher=True/False only changes the returned config
    # SHAPE (launcher stub vs direct runtime config), not which servers pass
    # the grant check. This proves that gate admits the identical server set
    # regardless of which provider-facing wrapper calls it -- the actual
    # cross-provider parity claim, not just "no separate reimplementation".
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    claude_configs = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    codex_gemini_configs = extension_store.native_mcp_launcher_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID in claude_configs and SERVER_ID in codex_gemini_configs
    print(f"{OK if ok else FAIL} grant gate admits the same server set for Claude's wrapper and Codex/Gemini's shared wrapper "
          f"(claude={list(claude_configs)}, codex_gemini={list(codex_gemini_configs)})")
    _cleanup()
    return ok


def test_grant_rejects_ambient_unsafe_server_even_when_declared() -> bool:
    # D3: the old kind="mcp" flag path proved a session-bound, ambient-
    # UNSAFE server (requires_backend_auth=True) could not be exposed
    # ambiently, even if someone tried. That property still lives in
    # native_mcp_declarations (gated on _native_harness_eligible before a
    # declaration is even built) but had no direct lock in the new grant
    # model -- declaring it in permissions.native_mcp does not make it
    # eligible; eligibility is a property of the entrypoint item itself.
    unsafe_server = "session-control"
    package = _record()
    manifest = extension_store.get_extension(EXT_ID)["manifest"]
    manifest["entrypoints"]["mcp"].append({
        "name": unsafe_server, "python": "mcp/server.py", "user_facing": False,
        "bare_allowed": True, "requires_backend_auth": True, "ambient_native": True,
    })
    manifest["permissions"]["native_mcp"][unsafe_server] = ["global"]
    data = extension_store._load()
    data["extensions"][EXT_ID]["manifest"] = manifest
    extension_store._save(data)
    try:
        extension_store.grant_native_mcp_server(EXT_ID, unsafe_server, "global")
        ok = False
    except extension_store.ExtensionError:
        ok = True
    print(f"{OK if ok else FAIL} grant_native_mcp_server rejects a session-bound/ambient-unsafe server "
          f"even when declared in permissions.native_mcp")
    _cleanup()
    return ok


def test_project_grant_only_resolves_for_its_own_project() -> bool:
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "project", project_path="/tmp/proj-a")
    in_project = extension_store.resolve_native_mcp_servers_for_context(project_path="/tmp/proj-a")
    other_project = extension_store.resolve_native_mcp_servers_for_context(project_path="/tmp/proj-b")
    no_project = extension_store.resolve_native_mcp_servers_for_context()
    ok = (
        f"{EXT_ID}:{SERVER_ID}" in in_project
        and f"{EXT_ID}:{SERVER_ID}" not in other_project
        and f"{EXT_ID}:{SERVER_ID}" not in no_project
    )
    print(f"{OK if ok else FAIL} project grant only resolves under its own project_path "
          f"(in={list(in_project)}, other={list(other_project)}, none={list(no_project)})")
    _cleanup()
    return ok


def test_project_grant_does_not_inherit_into_subdirectory() -> bool:
    # Exact-match-only, fail-closed by design (see native_mcp_grants.
    # project_target's docstring): a session cwd'd into
    # <granted-project>/subdir does NOT inherit the parent project's grant.
    import tempfile
    project_root = tempfile.mkdtemp(prefix="bc-test-native-mcp-project-root-")
    subdir = os.path.join(project_root, "packages", "api")
    os.makedirs(subdir, exist_ok=True)
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "project", project_path=project_root)
    in_root = extension_store.resolve_native_mcp_servers_for_context(project_path=project_root)
    in_subdir = extension_store.resolve_native_mcp_servers_for_context(project_path=subdir)
    ok = f"{EXT_ID}:{SERVER_ID}" in in_root and f"{EXT_ID}:{SERVER_ID}" not in in_subdir
    print(f"{OK if ok else FAIL} a project grant does not inherit into a subdirectory of the granted project "
          f"(root={list(in_root)}, subdir={list(in_subdir)})")
    _cleanup()
    return ok


def test_manifest_update_invalidates_outstanding_grant() -> bool:
    _record(args=("mcp/server.py",))
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    before = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    # Extension "ships an update": re-record with the SAME id but a manifest
    # change touching the launcher-derived command args (server_id's declared
    # eligible scopes narrowed to invalidate the previous grant's shape).
    _cleanup_record_only()
    _record(scopes=("project",))  # server_id no longer eligible for "global"
    after = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID in before and SERVER_ID not in after
    print(f"{OK if ok else FAIL} stale grant stops resolving once the manifest declaration changes "
          f"(before={list(before)}, after={list(after)})")
    _cleanup()
    return ok


def _cleanup_record_only() -> None:
    data = extension_store._load()
    data["extensions"].pop(EXT_ID, None)
    extension_store._save(data)


def test_server_file_content_change_invalidates_grant_with_identical_manifest() -> bool:
    # BL4's exact named scenario: an extension update rewrites server.py's
    # actual behavior while its manifest (id, version, permissions.native_mcp
    # scopes) stays byte-identical -- the same install_path, only the file
    # contents on disk differ. A version-string-only digest would miss this
    # entirely; the whole-package content hash must not.
    package = _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    before = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    (package / "mcp" / "server.py").write_text("print('a materially different implementation')\n", encoding="utf-8")
    after = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID in before and SERVER_ID not in after
    print(f"{OK if ok else FAIL} a server.py content change invalidates the grant even though the manifest is untouched "
          f"(before={list(before)}, after={list(after)})")
    _cleanup()
    return ok


def test_grant_survives_no_mirror_step_and_resolves_immediately() -> bool:
    # The ambient provider-config mirror was removed; grant_native_mcp_server
    # now persists the grant and returns with no out-of-BA fan-out. The grant
    # must be immediately resolvable through the per-session runtime bridge
    # (resolve_native_mcp_servers_for_context), which is the only remaining
    # authority on what's active.
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    grant_survived = len(native_mcp_grants.list_grants(extension_id=EXT_ID)) == 1
    resolved = extension_store.resolve_native_mcp_servers_for_context()
    ok = grant_survived and f"{EXT_ID}:{SERVER_ID}" in resolved
    print(f"{OK if ok else FAIL} grant persists and resolves immediately with no mirror step "
          f"(survived={grant_survived}, resolved={list(resolved)})")
    _cleanup()
    return ok


def test_uninstall_removes_grants() -> bool:
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    ok_before = len(native_mcp_grants.list_grants(extension_id=EXT_ID)) == 1
    extension_store.uninstall(EXT_ID)
    ok_after = native_mcp_grants.list_grants(extension_id=EXT_ID) == []
    ok = ok_before and ok_after
    print(f"{OK if ok else FAIL} uninstall removes all grants for the extension "
          f"(before={ok_before}, after_empty={ok_after})")
    native_mcp_grants.remove_grants_for_extension(EXT_ID)
    return ok


def test_disabled_extension_grant_resolves_to_nothing() -> bool:
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    extension_store.set_enabled(EXT_ID, False)
    configs = extension_store.native_mcp_server_configs({}, user_facing=False, bare=False)
    ok = SERVER_ID not in configs
    print(f"{OK if ok else FAIL} disabling the extension (record.enabled=False) makes its grant resolve to nothing (got {list(configs)})")
    _cleanup()
    return ok


def test_config_disabled_builtin_extension_grant_resolves_to_nothing() -> bool:
    # Distinct disablement mechanism from the test above: config_store's
    # per-run disabled_builtin_extensions list, checked inside
    # _all_native_mcp_declarations -- NOT the same guard as record.enabled.
    # Caught via mutation testing that the test above does not exercise this
    # path at all (removing this specific guard left it green).
    import config_store
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    config_store.set_disabled_builtin_extensions([EXT_ID])
    try:
        resolved = extension_store.resolve_native_mcp_servers_for_context()
    finally:
        config_store.set_disabled_builtin_extensions([])
    ok = f"{EXT_ID}:{SERVER_ID}" not in resolved
    print(f"{OK if ok else FAIL} config_store.disabled_builtin_extensions makes the grant resolve to nothing (got {list(resolved)})")
    _cleanup()
    return ok


def test_revoke_raises_on_unresolvable_project_path_instead_of_silently_coercing() -> bool:
    # BL2: revoke_native_mcp_server used to coerce a failed project_target(...)
    # to "" (the global sentinel) -- a revocation that silently doesn't
    # revoke the intended project grant. The specific gap was a NON-EMPTY
    # path that project_store._normalize rejects (empty path is caught
    # earlier by the "project_path is required" check) -- a NUL byte makes
    # Path.resolve() raise, giving None back from project_target with a
    # truthy project_path.
    _record()
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "project", project_path="/tmp/proj-a")
    try:
        extension_store.revoke_native_mcp_server(EXT_ID, SERVER_ID, "project", project_path="/tmp/x\x00y")
        ok = False
    except extension_store.ExtensionError:
        ok = True
    still_granted = f"{EXT_ID}:{SERVER_ID}" in extension_store.resolve_native_mcp_servers_for_context(project_path="/tmp/proj-a")
    ok = ok and still_granted
    print(f"{OK if ok else FAIL} revoke_native_mcp_server raises (not silently no-ops) on an unresolvable non-empty project_path, "
          f"and the real grant is untouched (still_granted={still_granted})")
    _cleanup()
    return ok


def test_grant_rejects_undeclared_scope() -> bool:
    _record(scopes=("global",))
    try:
        extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "project", project_path="/tmp/x")
        ok = False
    except extension_store.ExtensionError:
        ok = True
    print(f"{OK if ok else FAIL} grant_native_mcp_server rejects a scope the manifest didn't declare")
    _cleanup()
    return ok


def test_grant_rejects_undeclared_server_id() -> bool:
    _record()
    try:
        extension_store.grant_native_mcp_server(EXT_ID, "no-such-server", "global")
        ok = False
    except extension_store.ExtensionError:
        ok = True
    print(f"{OK if ok else FAIL} grant_native_mcp_server rejects a server_id the manifest doesn't declare")
    _cleanup()
    return ok


def test_revoke_removes_only_the_targeted_grant_leaving_siblings_intact() -> bool:
    package = _record()
    other_server = "sibling"
    manifest = extension_store.get_extension(EXT_ID)["manifest"]
    manifest["entrypoints"]["mcp"].append({
        "name": other_server, "python": "mcp/server.py", "user_facing": False,
        "bare_allowed": True, "requires_backend_auth": False, "ambient_native": True,
    })
    manifest["permissions"]["native_mcp"][other_server] = ["global"]
    data = extension_store._load()
    data["extensions"][EXT_ID]["manifest"] = manifest
    extension_store._save(data)
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    extension_store.grant_native_mcp_server(EXT_ID, other_server, "global")
    removed = extension_store.revoke_native_mcp_server(EXT_ID, SERVER_ID, "global")
    remaining = native_mcp_grants.list_grants(extension_id=EXT_ID)
    ok = removed and {g.server_id for g in remaining} == {other_server}
    print(f"{OK if ok else FAIL} revoke_native_mcp_server removes only the targeted grant, sibling untouched "
          f"(removed={removed}, remaining={[g.server_id for g in remaining]})")
    _cleanup()
    return ok


def test_grant_rejects_session_and_turn_scope_in_pr1() -> bool:
    _record(scopes=("global", "project", "session", "turn"))
    ok = True
    for scope in ("session", "turn"):
        try:
            extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, scope)
            ok = False
        except extension_store.ExtensionError:
            pass
    print(f"{OK if ok else FAIL} grant_native_mcp_server rejects session/turn scope (deferred to PR3)")
    _cleanup()
    return ok


def test_set_native_harness_exposed_routes_mcp_kind_through_grants() -> bool:
    _record()
    assert f"{EXT_ID}:{SERVER_ID}" not in extension_store.resolve_native_mcp_servers_for_context()
    granted = extension_store.set_native_harness_exposed(EXT_ID, "mcp", SERVER_ID, True)
    after_grant = f"{EXT_ID}:{SERVER_ID}" in extension_store.resolve_native_mcp_servers_for_context()
    revoked = extension_store.set_native_harness_exposed(EXT_ID, "mcp", SERVER_ID, False)
    after_revoke = f"{EXT_ID}:{SERVER_ID}" in extension_store.resolve_native_mcp_servers_for_context()
    ok = granted is True and after_grant and revoked is False and not after_revoke
    print(f"{OK if ok else FAIL} set_native_harness_exposed(kind='mcp') creates/revokes the global grant "
          f"(granted={after_grant}, revoked={not after_revoke})")
    _cleanup()
    return ok


def test_set_native_harness_exposed_mcp_fails_closed_for_undeclared_server() -> bool:
    # An entrypoint without a permissions.native_mcp declaration must not be
    # grantable through the exposure flag either -- same fail-closed gate as
    # grant_native_mcp_server itself.
    _record(scopes=())
    try:
        extension_store.set_native_harness_exposed(EXT_ID, "mcp", SERVER_ID, True)
        ok = False
    except extension_store.ExtensionError:
        ok = True
    print(f"{OK if ok else FAIL} set_native_harness_exposed(kind='mcp') fails closed for an undeclared server")
    _cleanup()
    return ok


def test_extension_harness_additions_reports_grant_state() -> bool:
    _record()
    record = extension_store.get_extension(EXT_ID)
    before = extension_store.extension_harness_additions(record)
    before_entry = next(item for item in before if item["kind"] == "mcp" and item["name"] == SERVER_ID)
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    record = extension_store.get_extension(EXT_ID)
    after = extension_store.extension_harness_additions(record)
    after_entry = next(item for item in after if item["kind"] == "mcp" and item["name"] == SERVER_ID)
    ok = before_entry["native_exposed"] is False and after_entry["native_exposed"] is True
    print(f"{OK if ok else FAIL} extension_harness_additions reflects grant state "
          f"(before={before_entry['native_exposed']}, after={after_entry['native_exposed']})")
    _cleanup()
    return ok


def test_resolve_native_mcp_server_config_matches_grant_state() -> bool:
    _record()
    record = extension_store.get_extension(EXT_ID)
    before = extension_store.resolve_native_mcp_server_config(
        extension_id=EXT_ID, server_name=SERVER_ID, inputs={},
    )
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    after = extension_store.resolve_native_mcp_server_config(
        extension_id=EXT_ID, server_name=SERVER_ID, inputs={},
    )
    ok = before is None and after is not None
    print(f"{OK if ok else FAIL} resolve_native_mcp_server_config gates on the grant store "
          f"(before={before}, after_is_none={after is None})")
    _cleanup()
    return ok


def main_run() -> int:
    tests = [
        test_permission_declaration_round_trips_through_manifest_validation,
        test_symlink_escaping_install_root_fails_closed,
        test_grant_global_makes_server_resolve_in_native_delivery_configs,
        test_no_grant_omits_server_from_native_delivery_configs,
        test_grant_gate_admits_identical_server_set_across_provider_wrappers,
        test_bytecode_cache_churn_does_not_false_invalidate_grant,
        test_venv_dependency_change_does_invalidate_grant,
        test_standalone_pyc_without_py_sibling_is_hashed,
        test_grant_rejects_ambient_unsafe_server_even_when_declared,
        test_project_grant_only_resolves_for_its_own_project,
        test_project_grant_does_not_inherit_into_subdirectory,
        test_manifest_update_invalidates_outstanding_grant,
        test_server_file_content_change_invalidates_grant_with_identical_manifest,
        test_grant_survives_no_mirror_step_and_resolves_immediately,
        test_uninstall_removes_grants,
        test_disabled_extension_grant_resolves_to_nothing,
        test_config_disabled_builtin_extension_grant_resolves_to_nothing,
        test_grant_rejects_undeclared_scope,
        test_grant_rejects_undeclared_server_id,
        test_revoke_removes_only_the_targeted_grant_leaving_siblings_intact,
        test_revoke_raises_on_unresolvable_project_path_instead_of_silently_coercing,
        test_grant_rejects_session_and_turn_scope_in_pr1,
        test_set_native_harness_exposed_routes_mcp_kind_through_grants,
        test_set_native_harness_exposed_mcp_fails_closed_for_undeclared_server,
        test_extension_harness_additions_reports_grant_state,
        test_resolve_native_mcp_server_config_matches_grant_state,
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
    print(f"\n{n_pass}/{n_total} native_mcp_grant_wiring tests passed")
    import shutil
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    shutil.rmtree(_TMP_OS_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())

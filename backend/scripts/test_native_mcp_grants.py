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

import logging
import os
import sys
from contextlib import contextmanager

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


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _captured_warnings():
    """Single owner of the 'assert this store logged a warning' pattern."""
    handler = _ListHandler()
    g.logger.addHandler(handler)
    g.logger.setLevel(logging.WARNING)
    try:
        yield handler.records
    finally:
        g.logger.removeHandler(handler)


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
    from json_store import write_json_durable

    decl = _decl()
    g.add_grant(extension_id="ext-w", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    write_json_durable(g._store_path(), {
        "schema_version": g.SCHEMA_VERSION + 999,
        "grants": [{"extension_id": "ext-w", "server_id": "a", "scope": "global", "target": "", "digest": decl.digest(), "created_at": "t"}],
    })

    with _captured_warnings() as records:
        grants = g.list_grants()
    warned = any("schema_version mismatch" in r.getMessage() for r in records)
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
    from json_store import write_json_durable

    write_json_durable(g._store_path(), {
        "schema_version": g.SCHEMA_VERSION,
        "grants": [{"extension_id": "ext-mal", "server_id": "a", "scope": "not-a-real-scope", "target": "", "digest": "d", "created_at": "t"}],
    })
    with _captured_warnings() as records:
        grants = g.list_grants()
    warned = any("dropping" in r.getMessage() for r in records)
    assert grants == []
    assert warned, "an unparseable grant row must be logged, not silently dropped"
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})


# --------------------------------------------------------------------------- #
# Fail-closed corruption branches + the untested query/mutation surface.
# Every branch below is security-relevant: this store persists grants that
# gate native MCP server launches, so a corrupted file or a forgotten guard
# must fail CLOSED (no grant resolves) and be loud about it.
# --------------------------------------------------------------------------- #

def test_read_non_list_grants_field_treated_as_empty_and_warned():
    from json_store import write_json_durable

    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": "not-a-list"})
    with _captured_warnings() as records:
        grants = g.list_grants()
    assert grants == []
    assert any("non-list" in r.getMessage() for r in records), \
        "a non-list grants field must warn, not silently resolve to empty"
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})


def test_parse_grant_drops_rows_with_missing_keys_or_wrong_type():
    # The except (KeyError, TypeError) arm: a row missing a required key, and
    # a row that isn't a dict at all, are both dropped with a warning rather
    # than crashing the whole reconcile. The next write rewrites only what
    # parsed, so the malformed rows are permanently gone unless this logs.
    from json_store import write_json_durable

    write_json_durable(g._store_path(), {
        "schema_version": g.SCHEMA_VERSION,
        "grants": [
            {"extension_id": "ext-miss", "server_id": "a", "scope": "global", "target": "", "created_at": "t"},  # missing digest -> KeyError
            ["not", "a", "dict"],  # raw["scope"] -> TypeError
        ],
    })
    with _captured_warnings() as records:
        grants = g.list_grants()
    assert grants == []
    assert len(records) >= 2, f"each unparseable row should warn once: {len(records)}"
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})


def test_list_grants_filters_by_scope_and_target():
    decl = _decl()
    g.add_grant(extension_id="ext-f", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    sess_target = "sess-1"
    g.add_grant(extension_id="ext-f", server_id="b", scope="session", target=sess_target, digest=decl.digest(), created_at="t")

    assert [gr.server_id for gr in g.list_grants(extension_id="ext-f", scope="global")] == ["a"]
    assert [gr.server_id for gr in g.list_grants(extension_id="ext-f", target=sess_target)] == ["b"]
    assert [gr.server_id for gr in g.list_grants(scope="session", target=sess_target)] == ["b"]
    g.remove_grants_for_extension("ext-f")


def test_add_grant_rejects_invalid_scope():
    decl = _decl()
    with pytest.raises(ValueError):
        g.add_grant(extension_id="ext-bad", server_id="a", scope="galaxy", target="", digest=decl.digest(), created_at="t")
    assert g.list_grants(extension_id="ext-bad") == []


def test_remove_grant_returns_false_when_absent():
    # The not-found arm must NOT write the store and must report False.
    decl = _decl()
    g.add_grant(extension_id="ext-rm", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    assert g.remove_grant(extension_id="ext-rm", server_id="a", scope="global", target="") is True
    # Second removal of the same key: nothing to remove.
    assert g.remove_grant(extension_id="ext-rm", server_id="a", scope="global", target="") is False
    assert g.remove_grant(extension_id="ext-rm", server_id="missing", scope="global", target="") is False
    assert g.list_grants(extension_id="ext-rm") == []


def test_remove_grants_for_target_noop_when_none_match():
    decl = _decl()
    g.add_grant(extension_id="ext-nt", server_id="a", scope="global", target="", digest=decl.digest(), created_at="t")
    # No grant has scope=session target=sess-x -> removed==0, store NOT rewritten.
    assert g.remove_grants_for_target("session", "sess-x") == 0
    assert len(g.list_grants(extension_id="ext-nt")) == 1
    g.remove_grants_for_extension("ext-nt")


def test_resolve_session_scope_matches_only_own_session():
    decl = _decl()
    g.add_grant(extension_id="ext-se", server_id="a", scope="session", target="sess-1", digest=decl.digest(), created_at="t")
    declarations = {("ext-se", "a"): decl}

    own = g.resolve_native_mcp_servers(active_declarations=declarations, session_id="sess-1")
    other = g.resolve_native_mcp_servers(active_declarations=declarations, session_id="sess-2")
    no_session = g.resolve_native_mcp_servers(active_declarations=declarations)
    assert "ext-se:a" in own
    assert "ext-se:a" not in other
    assert "ext-se:a" not in no_session
    g.remove_grants_for_extension("ext-se")


def test_resolve_turn_scope_matches_only_own_turn():
    decl = _decl()
    target = g.turn_target("root-1", "turn-1")
    assert target is not None
    g.add_grant(extension_id="ext-tu", server_id="a", scope="turn", target=target, digest=decl.digest(), created_at="t")
    declarations = {("ext-tu", "a"): decl}

    own = g.resolve_native_mcp_servers(active_declarations=declarations, root_id="root-1", turn_id="turn-1")
    other = g.resolve_native_mcp_servers(active_declarations=declarations, root_id="root-1", turn_id="turn-9")
    assert "ext-tu:a" in own
    assert "ext-tu:a" not in other
    g.remove_grants_for_extension("ext-tu")


def test_resolve_corrupted_scope_row_never_matches():
    # A scope outside VALID_SCOPES can only enter the store by direct file
    # corruption (add_grant rejects it). _parse_grant must drop it on read so
    # the resolver never sees it -- pin that drop: a bogus-scope row resolves
    # to nothing even when its declaration is active.
    from json_store import write_json_durable

    decl = _decl()
    write_json_durable(g._store_path(), {
        "schema_version": g.SCHEMA_VERSION,
        "grants": [{
            "extension_id": "ext-co", "server_id": "a", "scope": "galaxy", "target": "",
            "digest": decl.digest(), "created_at": "t",
        }],
    })
    resolved = g.resolve_native_mcp_servers(
        active_declarations={("ext-co", "a"): decl},
        session_id="sess-1", root_id="r1", turn_id="t1", project_path="/tmp/x",
    )
    assert resolved == {}
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})


def test_project_target_separator_guard_fires_even_when_normalize_lets_sep_through():
    # project_target's separator guard is defense-in-depth: it must reject a
    # forgeable key even if project_store._normalize happened to preserve the
    # separator char. _normalize normally rejects these paths, which would
    # leave this guard unexercised; pin it directly so a future relaxation
    # of _normalize can't silently re-open the forgeable-key path.
    import project_store

    sep = g._TARGET_SEP
    real_normalize = project_store._normalize

    def _leak_sep(path, node_id="primary"):
        if sep in str(path):
            return f"leaked{sep}normalized"
        return real_normalize(path, node_id)

    project_store._normalize = _leak_sep
    try:
        # Sep reaches the guard via the normalized component.
        assert g.project_target("primary", f"/tmp/x{sep}evil") is None
        # Sep reaches the guard via the resolved_node_id component.
        assert g.project_target(f"evil{sep}node", "/tmp/x") is None
    finally:
        project_store._normalize = real_normalize


def test_rewrite_project_paths_repairs_matching_targets_and_skips_the_rest():
    import project_store

    decl = _decl()
    old_path = "/tmp/proj-old"
    new_path = "/tmp/proj-new"
    old_norm = project_store._normalize(old_path, "primary")
    new_norm = project_store._normalize(new_path, "primary")
    assert old_norm is not None and new_norm is not None

    target = g.project_target("primary", old_path)
    assert target == f"primary{g._TARGET_SEP}{old_norm}"
    g.add_grant(extension_id="ext-rw", server_id="match", scope="project", target=target, digest=decl.digest(), created_at="t")
    # An unrelated global grant and an unrelated project grant on another node
    # must both be left untouched.
    g.add_grant(extension_id="ext-rw", server_id="glob", scope="global", target="", digest=decl.digest(), created_at="t")
    other_target = f"other-node{g._TARGET_SEP}{old_norm}"
    g.add_grant(extension_id="ext-rw", server_id="other", scope="project", target=other_target, digest=decl.digest(), created_at="t")

    g.rewrite_project_paths({("primary", old_norm): new_norm})

    grants = {gr.server_id: gr.target for gr in g.list_grants(extension_id="ext-rw")}
    assert grants["match"] == f"primary{g._TARGET_SEP}{new_norm}"
    assert grants["glob"] == ""
    assert grants["other"] == other_target  # different node, untouched
    g.remove_grants_for_extension("ext-rw")


def test_rewrite_project_paths_skips_identity_and_empty_rewrites():
    decl = _decl()
    target = g.project_target("primary", "/tmp/proj-stable")
    g.add_grant(extension_id="ext-id", server_id="a", scope="project", target=target, digest=decl.digest(), created_at="t")

    # Empty rewrites map -> early return, no write.
    g.rewrite_project_paths({})
    assert [gr.target for gr in g.list_grants(extension_id="ext-id")] == [target]
    # A rewrite that maps old->same is not a change; target stays.
    import project_store
    norm = project_store._normalize("/tmp/proj-stable", "primary")
    g.rewrite_project_paths({("primary", norm): norm})
    assert [gr.target for gr in g.list_grants(extension_id="ext-id")] == [target]
    g.remove_grants_for_extension("ext-id")


def test_rewrite_project_paths_skips_project_rows_with_malformed_targets():
    # A project-scope row whose target isn't a valid "node\x1fpath" string
    # (only reachable via direct file corruption) must be skipped, not crash
    # the repair pass. Pins the continue at the malformed-target guard.
    from json_store import write_json_durable

    decl = _decl()
    write_json_durable(g._store_path(), {
        "schema_version": g.SCHEMA_VERSION,
        "grants": [
            {"extension_id": "ext-mf", "server_id": "ok", "scope": "project",
             "target": f"primary{g._TARGET_SEP}/tmp/old", "digest": decl.digest(), "created_at": "t"},
            # malformed: project scope but target has no separator -> skipped
            {"extension_id": "ext-mf", "server_id": "bad", "scope": "project",
             "target": "no-separator-here", "digest": decl.digest(), "created_at": "t"},
        ],
    })
    g.rewrite_project_paths({("primary", "/tmp/old"): "/tmp/new"})
    grants = {gr.server_id: gr.target for gr in g.list_grants(extension_id="ext-mf")}
    assert grants["ok"] == f"primary{g._TARGET_SEP}/tmp/new"
    assert grants["bad"] == "no-separator-here"  # untouched
    write_json_durable(g._store_path(), {"schema_version": g.SCHEMA_VERSION, "grants": []})


def test_project_target_returns_none_when_project_store_rejects_path():
    # project_target delegates normalization to project_store and fails closed
    # (None) when that normalization itself rejects the path -- e.g. an empty
    # path. This is the fail-closed arm separate from the separator guard.
    assert g.project_target("primary", "") is None


def test_validate_scope_target_is_noop_for_unknown_scope():
    # _validate_scope_target enforces scope<->target shape for the four valid
    # scopes; an unknown scope is a no-op here (no raise) because scope
    # validity is gated upstream in add_grant. Pin that contract: an unknown
    # scope must not raise a misleading error out of this helper.
    g._validate_scope_target("galaxy", "anything")  # no raise


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

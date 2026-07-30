"""Scoped grant store for extension-declared native MCP servers.

Design (converged over 2 rounds of adversarial review, session 213c938c —
see PR1 scope note at the bottom of this docstring):

An extension's manifest declares native MCP servers it wants to expose, and
the scopes each is eligible for (`global`, `project`, `session`, `turn`) —
one `permissions.native_mcp` review at install time. A GRANT is a separate,
narrower record: "extension X's server Y is actually active at scope S for
target T." Global/project grants are created at install time from the
manifest declaration (the install review IS the grant for those two — no
extra step). Session/turn grants are created at runtime by the extension
itself (PR3, not this module) — but ONLY for scopes the manifest already
declared eligibility for; this module never accepts raw command/args at
grant time, only a `declared_id` resolved against the extension's OWN
current manifest.

Security property (fail-closed, checked by the resolver, not by grant
creation): a grant is bound to a DIGEST of the server declaration at the
moment the grant was created (command, args, env var *names* — never
values, so secrets are never hashed/persisted here — and eligible scopes).
If the extension updates and its declaration for that server_id changes,
the digest no longer matches and the grant silently stops resolving until
re-approved. This closes the "extension ships v1.1 with a different command
under an already-approved grant" escalation path — a grant authorizes a
specific reviewed thing, not a name.

Lifecycle property (separate from the security property — a resolve-time
digest/target mismatch is NOT a substitute for actually deleting grants):
- extension uninstall -> `remove_grants_for_extension`
- session end -> `remove_grants_for_target("session", session_id)` (caller:
  session lifecycle code, not this module)
- turn end -> `remove_grants_for_target("turn", ...)` (caller: post-turn
  lifecycle code)
This module only stores and queries; it does not itself hook into
extension/session/turn lifecycle events — callers own that wiring, same
separation `extension_mcp.py` already has from `extension_store.py`.

PR1 scope: this module supports `global` and `project` grants end-to-end
(both install-time, both reachable through real code paths). `session` and
`turn` scopes are modeled in the schema (valid `scope` values) but nothing
in this codebase creates them yet — that's `activate_mcp_server`/
`deactivate_mcp_server`, deliberately deferred to its own PR (PR3) since
it's the actual runtime-facing security surface, reviewed in isolation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from json_store import read_json, write_json_durable
from paths import ba_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
VALID_SCOPES = ("global", "project", "session", "turn")

# In-process only. Safe as long as it stays that way: every writer to this
# store today is a function in extension_store.py (grant_native_mcp_server,
# revoke_native_mcp_server, uninstall), reachable only from backend-API code
# running in the single backend process -- no detached runner, spawned turn
# subprocess, or CLI process writes here. If a future PR adds a write path
# reachable from outside the backend process (e.g. a runner-side
# activate_mcp_server for session/turn scope in PR3), this RLock stops being
# sufficient and the read-modify-write in add_grant/remove_grant* needs real
# cross-process file locking, not just an atomic replace on the final write.
_LOCK = threading.RLock()
_TARGET_SEP = "\x1f"  # unit separator — never appears in a node_id/path/session_id/turn_id


def _store_path() -> Path:
    return ba_home() / "extensions" / "native_mcp_grants.json"


@dataclass(frozen=True)
class ServerDeclaration:
    """The reviewable thing a manifest declares for one native MCP server —
    exactly what an install-time permission review must show the human
    (command + args), plus which scopes it's eligible for.

    `package_fingerprint` is a whole-package content hash of the installed
    extension (extension_store._native_mcp_package_content_fingerprint --
    see that function for exactly what's hashed/excluded and why), not a
    launcher detail: command/args are always the fixed launcher stub
    (extension_mcp._launcher_command), so they never change
    between an extension's versions -- without an artifact hash in the
    digest, an extension shipping a materially different server.py under an
    already-approved grant would produce the SAME digest and the grant would
    silently keep resolving. This measures the installed files themselves,
    not a version string the same party could leave stale -- there is no
    "re-present permissions.native_mcp for review on update" flow in this
    codebase, so this is the SOLE gate on grant survival across an update,
    not a belt-and-braces check on top of one."""

    command: str
    args: tuple[str, ...]
    env_keys: tuple[str, ...]
    scopes: tuple[str, ...]
    package_fingerprint: str

    def digest(self) -> str:
        # `command` is deliberately excluded: it is the interpreter path of
        # whichever platform process computes the declaration (the backend's
        # dependency-plan venv vs. an ambient launcher's .venv python), never
        # anything the extension controls. Binding it made one grant record
        # unable to resolve in both contexts on the same machine — the grant
        # silently stopped resolving wherever sys.executable differed from
        # the creator's. Everything extension-controlled stays bound: args
        # (launcher stub + identity), env key names, scopes, and the
        # whole-package content fingerprint.
        payload = json.dumps(
            {
                "args": list(self.args),
                "env_keys": sorted(self.env_keys),
                "scopes": sorted(self.scopes),
                "package_fingerprint": self.package_fingerprint,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Grant:
    extension_id: str
    server_id: str
    scope: str
    target: str
    digest: str
    created_at: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.extension_id, self.server_id, self.scope, self.target)


def project_target(node_id: str, project_path: str) -> str | None:
    """Project identity as `project_store` already treats it canonically —
    `(node_id, normalized_path)`, not a raw/fragile cwd string. Calls
    `project_store`'s OWN normalization directly rather than reimplementing
    it, so a grant's target always matches however `project_store` itself
    would key the same path — one owner for that logic, not two copies
    that could drift. Returns None for whatever `project_store._normalize`
    itself rejects (empty/unresolvable path).

    Subdirectory inheritance is this feature's own semantic, not
    `project_store`'s: a project-scope grant matches EXACT normalized paths
    only. A session cwd'd into `<granted-project>/packages/api` does NOT
    inherit the parent project's grant -- it normalizes to a different
    target string entirely. Deliberate, fail-closed default (an explicit
    grant authorizes the reviewed project, not an unbounded subtree under
    it); widening to prefix/subtree matching is a real design question for
    a future PR, not an oversight here.

    Fails closed (returns None) if either component contains `_TARGET_SEP`
    itself: on POSIX any byte except `/` and NUL is legal in a filename, so
    an unescaped path COULD contain the separator and forge a target string
    that collides with a different node's grant. This is a security key
    built by string concatenation of a value that ultimately traces back to
    user-controlled input (a project path); reject rather than trust."""
    import project_store

    resolved_node_id = node_id or "primary"
    normalized = project_store._normalize(project_path, resolved_node_id)
    if normalized is None:
        return None
    if _TARGET_SEP in resolved_node_id or _TARGET_SEP in normalized:
        return None
    return f"{resolved_node_id}{_TARGET_SEP}{normalized}"


def turn_target(root_id: str, turn_id: str) -> str | None:
    """Fails closed (returns None) if either component contains
    `_TARGET_SEP` -- same forgeable-key concern as `project_target`."""
    if _TARGET_SEP in root_id or _TARGET_SEP in turn_id:
        return None
    return f"{root_id}{_TARGET_SEP}{turn_id}"


def _read() -> dict[str, Any]:
    default: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "grants": []}
    data = read_json(_store_path(), default)
    if data.get("schema_version") != SCHEMA_VERSION:
        # No migrations supported for this store (project convention) — an
        # unexpected version is a corrupt/foreign file, not something to
        # silently coerce. Fail closed: treat as empty rather than trust it.
        # Loud, not silent: an empty store here means every outstanding
        # native MCP grant vanishes at once (servers drop out of provider
        # configs, settings rows flip to "not granted") with no other
        # signal, so this MUST be observable, not just safe.
        logger.warning(
            "native_mcp_grants store schema_version mismatch (found %r, expected %r) at %s "
            "-- treating as empty; every outstanding native MCP grant is now unresolved "
            "until this file is inspected/removed",
            data.get("schema_version"), SCHEMA_VERSION, _store_path(),
        )
        return {"schema_version": SCHEMA_VERSION, "grants": []}
    if not isinstance(data.get("grants"), list):
        logger.warning(
            "native_mcp_grants store at %s has a non-list 'grants' field -- treating as empty; "
            "every outstanding native MCP grant is now unresolved until this file is inspected/removed",
            _store_path(),
        )
        return {"schema_version": SCHEMA_VERSION, "grants": []}
    return data


def _write(data: dict[str, Any]) -> None:
    write_json_durable(_store_path(), data)


def _parse_grant(raw: dict[str, Any]) -> Grant | None:
    """Returns None for a row this store can't parse. Every write path
    (add_grant/remove_grant*) parses the WHOLE list and rewrites only what
    parsed -- an unparseable row is silently and permanently dropped by the
    very next unrelated grant operation unless this logs it. Loud, not
    silent, matching `_read`'s fail-closed branches."""
    try:
        scope = str(raw["scope"])
        if scope not in VALID_SCOPES:
            logger.warning("native_mcp_grants: dropping a grant row with an unrecognized scope %r: %r", scope, raw)
            return None
        return Grant(
            extension_id=str(raw["extension_id"]),
            server_id=str(raw["server_id"]),
            scope=scope,
            target=str(raw["target"]),
            digest=str(raw["digest"]),
            created_at=str(raw["created_at"]),
        )
    except (KeyError, TypeError) as exc:
        logger.warning("native_mcp_grants: dropping an unparseable grant row (%s): %r", exc, raw)
        return None


def list_grants(
    *, extension_id: str | None = None, scope: str | None = None, target: str | None = None,
) -> list[Grant]:
    with _LOCK:
        grants = [g for g in (_parse_grant(r) for r in _read()["grants"]) if g is not None]
    if extension_id is not None:
        grants = [g for g in grants if g.extension_id == extension_id]
    if scope is not None:
        grants = [g for g in grants if g.scope == scope]
    if target is not None:
        grants = [g for g in grants if g.target == target]
    return grants


def rewrite_project_paths(rewrites: dict[tuple[str, str], str]) -> None:
    if not rewrites:
        return
    with _LOCK:
        data = _read()
        changed = False
        for raw in data["grants"]:
            if not isinstance(raw, dict) or raw.get("scope") != "project":
                continue
            target = raw.get("target")
            if not isinstance(target, str) or _TARGET_SEP not in target:
                continue
            node_id, old_path = target.split(_TARGET_SEP, 1)
            repaired = rewrites.get((node_id, old_path))
            if repaired and repaired != old_path:
                raw["target"] = f"{node_id}{_TARGET_SEP}{repaired}"
                changed = True
        if changed:
            _write(data)


def _validate_scope_target(scope: str, target: str) -> None:
    """Enforces the scope<->target shape this store's OWN resolver assumes
    (see resolve_native_mcp_servers' scope-matching branches) at the write
    boundary, not just at today's single caller (extension_store.
    grant_native_mcp_server, which already gets this right) -- add_grant is
    this store's public API, and PR3 adds a second caller from the
    runtime-activation surface. Defense in depth where the invariant
    actually lives, not duplicated at every caller."""
    if scope == "global":
        if target != "":
            raise ValueError(f"scope='global' requires target='' (the global sentinel), got {target!r}")
    elif scope in ("project", "turn"):
        if not target or _TARGET_SEP not in target:
            raise ValueError(f"scope={scope!r} requires a non-empty target built by {scope}_target(...), got {target!r}")
    elif scope == "session":
        if not target:
            raise ValueError("scope='session' requires a non-empty target (the session id)")


def add_grant(
    *, extension_id: str, server_id: str, scope: str, target: str, digest: str, created_at: str,
) -> Grant:
    if scope not in VALID_SCOPES:
        raise ValueError(f"invalid scope {scope!r}, must be one of {VALID_SCOPES}")
    _validate_scope_target(scope, target)
    grant = Grant(
        extension_id=extension_id, server_id=server_id, scope=scope,
        target=target, digest=digest, created_at=created_at,
    )
    with _LOCK:
        data = _read()
        grants = [g for g in (_parse_grant(r) for r in data["grants"]) if g is not None]
        grants = [g for g in grants if g.key() != grant.key()]
        grants.append(grant)
        data["grants"] = [
            {
                "extension_id": g.extension_id, "server_id": g.server_id, "scope": g.scope,
                "target": g.target, "digest": g.digest, "created_at": g.created_at,
            }
            for g in grants
        ]
        _write(data)
    return grant


def remove_grant(*, extension_id: str, server_id: str, scope: str, target: str) -> bool:
    key = (extension_id, server_id, scope, target)
    with _LOCK:
        data = _read()
        grants = [g for g in (_parse_grant(r) for r in data["grants"]) if g is not None]
        remaining = [g for g in grants if g.key() != key]
        if len(remaining) == len(grants):
            return False
        data["grants"] = [
            {
                "extension_id": g.extension_id, "server_id": g.server_id, "scope": g.scope,
                "target": g.target, "digest": g.digest, "created_at": g.created_at,
            }
            for g in remaining
        ]
        _write(data)
    return True


def remove_grants_for_extension(extension_id: str) -> int:
    with _LOCK:
        data = _read()
        grants = [g for g in (_parse_grant(r) for r in data["grants"]) if g is not None]
        remaining = [g for g in grants if g.extension_id != extension_id]
        removed = len(grants) - len(remaining)
        if removed:
            data["grants"] = [
                {
                    "extension_id": g.extension_id, "server_id": g.server_id, "scope": g.scope,
                    "target": g.target, "digest": g.digest, "created_at": g.created_at,
                }
                for g in remaining
            ]
            _write(data)
    return removed


def remove_grants_for_target(scope: str, target: str) -> int:
    with _LOCK:
        data = _read()
        grants = [g for g in (_parse_grant(r) for r in data["grants"]) if g is not None]
        remaining = [g for g in grants if not (g.scope == scope and g.target == target)]
        removed = len(grants) - len(remaining)
        if removed:
            data["grants"] = [
                {
                    "extension_id": g.extension_id, "server_id": g.server_id, "scope": g.scope,
                    "target": g.target, "digest": g.digest, "created_at": g.created_at,
                }
                for g in remaining
            ]
            _write(data)
    return removed


def resolve_native_mcp_servers(
    *,
    active_declarations: dict[tuple[str, str], ServerDeclaration],
    node_id: str = "primary",
    project_path: str | None = None,
    session_id: str | None = None,
    root_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """The one resolver every provider's per-turn/per-run injection point
    calls. Returns `{qualified_name: launcher_server_item}` — union of every
    grant whose scope target matches this context AND whose stored digest
    still matches the extension's CURRENT declaration (fail-closed: a
    manifest update invalidates outstanding grants for that server_id until
    re-approved, per the security property in this module's docstring).

    `active_declarations` must come from the caller's own currently-active,
    currently-enabled extension records (extension_store owns that; this
    module never imports it, mirroring how extension_mcp.py already takes
    `records` as a parameter instead of importing extension_store) — a
    disabled/uninstalled extension's declarations are simply absent from
    this dict, so its grants resolve to nothing without any extra check
    here.

    Qualified names are always `<extension_id>:<server_id>` — structurally
    impossible for an extension to shadow another extension's server or a
    user's own MCP server of the same short name."""
    from extension_mcp import launcher_server_item

    want_project_target = (
        project_target(node_id, project_path) if project_path else None
    )
    want_turn_target = (
        turn_target(root_id, turn_id) if (root_id and turn_id) else None
    )

    resolved: dict[str, dict[str, Any]] = {}
    for grant in list_grants():
        declaration = active_declarations.get((grant.extension_id, grant.server_id))
        if declaration is None:
            continue
        if grant.digest != declaration.digest():
            continue
        if grant.scope == "global":
            matched = True
        elif grant.scope == "project":
            matched = want_project_target is not None and grant.target == want_project_target
        elif grant.scope == "session":
            matched = session_id is not None and grant.target == session_id
        elif grant.scope == "turn":
            matched = want_turn_target is not None and grant.target == want_turn_target
        else:
            matched = False
        if not matched:
            continue
        qualified_name = f"{grant.extension_id}:{grant.server_id}"
        resolved[qualified_name] = launcher_server_item(
            grant.extension_id, grant.server_id,
            command=declaration.command, args=list(declaration.args),
        )
    return resolved

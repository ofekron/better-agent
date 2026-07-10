"""Locks the contract that every `coordinator.broadcast_global(...)`
call site in the backend uses a type that's in
`Coordinator.GLOBAL_EVENT_ALLOWLIST`.

Pre-fix the log showed:
    ERROR main: broadcast models_catalog_changed failed for ...
    ValueError: broadcast_global called with non-allowlisted type
    'models_catalog_changed'; ...

The `ValueError` is enforced by `coordinator.broadcast_global` to keep
cross-session pings disciplined. The throw was swallowed by the
`_models_catalog_refresher` background task — the frontend never got
the invalidation ping, silently breaking multi-tab convergence on
model-catalog refresh.

This test AST-walks every backend module that could call
`coordinator.broadcast_global(...)`:

  1. **String literals** — assert in `GLOBAL_EVENT_ALLOWLIST`.
  2. **f-strings** — must be registered in `FSTRING_EXPANSIONS` with
     the explicit list of expected expansions; assert each expansion
     is allowlisted. Failing-closed forces a developer making a new
     f-string broadcast to register it here (so the audit trail is
     explicit).
  3. **Non-literal first args** (e.g. `payload["type"]`) — must be
     registered in `KNOWN_DYNAMIC_CALLERS` with an audit-note. Any
     unregistered dynamic caller fails the test.

Run with:
    cd backend && .venv/bin/python scripts/test_global_allowlist_covers_runtime_broadcasts.py
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-allowlist-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

ALLOWLIST = Coordinator.GLOBAL_EVENT_ALLOWLIST

SCANNED_FILES = [
    Path(_BACKEND) / "main.py",
    Path(_BACKEND) / "startup_tasks.py",
    Path(_BACKEND) / "scheduler.py",
    Path(_BACKEND) / "session_ws_broadcaster.py",
    Path(_BACKEND) / "orchestrator.py",
    Path(_BACKEND) / "run_recovery.py",
    Path(_BACKEND) / "provider_config_sync_api.py",
    Path(_BACKEND) / "project_structure_edit_session.py",
    Path(_BACKEND) / "extension_api.py",
    Path(_BACKEND) / "session_search.py",
    Path(_BACKEND) / "task_assessor.py",
    Path(_BACKEND) / "task_runner.py",
]

# f-strings whose interpolations CANNOT be statically enumerated from
# the call's local scope alone. Each entry registers the explicit set
# of expansions the developer has audited. Adding a new f-string
# broadcast REQUIRES adding to this map.
FSTRING_EXPANSIONS: dict[str, list[str]] = {
    # `coordinator.broadcast_global(f"session_processing_{kind}", ...)`
    # `kind` is bound in `_emit_processing(kind, root_id)` at
    # session_manager.py:486-492; the only call sites at
    # session_manager.py:484 and :490 pass literal "started" and
    # "finished".
    "main.py:_emit_session_processing f'session_processing_{kind}'": [
        "session_processing_started",
        "session_processing_finished",
    ],
}

# Non-literal first-arg call sites that have been audited and confirmed
# to only produce allowlisted types in practice. Adding a new entry
# REQUIRES a one-line audit explanation.
KNOWN_DYNAMIC_CALLERS: dict[str, str] = {
    "orchestrator.py:broadcast_global": (
        "broadcast_global delegates its already-public event_type to the canonical "
        "schedule_global validation path"
    ),
    "main.py:_on_node_registration": (
        "_on_node_registration receives only node_registration_requested "
        "or node_registration_resolved from node_link.set_registration_listener"
    ),
    "main.py:_broadcast_install": (
        "_broadcast_install is registered as provider_setup callback; provider_setup "
        "emits only provider_install_progress/provider_install_finished"
    ),
    "session_search.py:_broadcast_global_later.<locals>._run": (
        "_broadcast_global_later is module-local and all call sites pass literal "
        "Ask session event types verified by the literal-site scan"
    ),
    # `_dispatch` is the generic fan-out used by SessionWSBroadcaster's
    # typed mapping. The payload's `"type"` field is hard-coded in the
    # mapping itself (search for `"type":` literals in
    # `on_change` and `_dispatch` callers); every literal is in
    # the allowlist by construction. The dispatcher is dead-end
    # plumbing — typing it dynamically here is OK.
    "session_ws_broadcaster.py:_dispatch": (
        "_dispatch fed by typed on_change mapping; payload['type'] is "
        "always a literal in the mapping construction"
    ),
}


def _enumerate_fstring(node: ast.JoinedStr) -> str:
    """Render the f-string's source-level template, e.g.
    `f"session_processing_{kind}"`. Used as the lookup key in
    FSTRING_EXPANSIONS."""
    parts: list[str] = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        elif isinstance(v, ast.FormattedValue):
            if isinstance(v.value, ast.Name):
                parts.append("{" + v.value.id + "}")
            else:
                parts.append("{...}")
        else:
            parts.append("{?}")
    return "f'" + "".join(parts) + "'"


def _function_qualname(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> str:
    names: list[str] = []
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(cur.name)
    if not names:
        return "<module>"
    return ".<locals>.".join(reversed(names))


def _collect_calls(tree: ast.Module) -> list[tuple[int, str, ast.expr]]:
    """Find every `XXX.broadcast_global(first_arg, ...)` call site."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found: list[tuple[int, str, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"broadcast_global", "schedule_global"}:
            continue
        if not node.args:
            continue
        found.append((node.lineno, _function_qualname(node, parents), node.args[0]))
    return found


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    # 1) Coverage check: no `broadcast_global(` outside SCANNED_FILES.
    backend_dir = Path(_BACKEND)
    py_files = [
        p for p in backend_dir.rglob("*.py")
        if "/scripts/" not in str(p) and ".venv/" not in str(p)
    ]
    scanned_str = {str(p.resolve()) for p in SCANNED_FILES}
    extra_callers: list[str] = []
    for p in py_files:
        if str(p.resolve()) in scanned_str:
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        if (
            ("broadcast_global(" in text and "def broadcast_global" not in text)
            or ("schedule_global(" in text and "def schedule_global" not in text)
        ):
            extra_callers.append(str(p.relative_to(backend_dir)))
    results.append(
        ("no unscanned `broadcast_global(` callers",
         not extra_callers, f"found: {extra_callers}"))

    # 2) Per-file: assert every call's first arg is allowlisted.
    bad: list[str] = []
    seen_fstrings: set[str] = set()
    seen_dynamic: set[str] = set()
    for fp in SCANNED_FILES:
        if not fp.exists():
            continue
        src = fp.read_text()
        tree = ast.parse(src, filename=str(fp))
        for lineno, qualname, arg in _collect_calls(tree):
            site = f"{fp.name}:{qualname}"
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value not in ALLOWLIST:
                    bad.append(
                        f"{site} → literal {arg.value!r} not in allowlist"
                    )
                continue
            if (
                isinstance(arg, ast.IfExp)
                and isinstance(arg.body, ast.Constant)
                and isinstance(arg.body.value, str)
                and isinstance(arg.orelse, ast.Constant)
                and isinstance(arg.orelse.value, str)
            ):
                for value in (arg.body.value, arg.orelse.value):
                    if value not in ALLOWLIST:
                        bad.append(
                            f"{site} → ternary literal {value!r} not in allowlist"
                        )
                continue
            if isinstance(arg, ast.JoinedStr):
                template = _enumerate_fstring(arg)
                key = f"{site} {template}"
                seen_fstrings.add(key)
                expansions = FSTRING_EXPANSIONS.get(key)
                if expansions is None:
                    bad.append(
                        f"{site} → f-string {template} NOT registered in "
                        f"FSTRING_EXPANSIONS; add the audited list of "
                        f"expansions to that map"
                    )
                    continue
                for v in expansions:
                    if v not in ALLOWLIST:
                        bad.append(
                            f"{site} → f-string {template} expansion "
                            f"{v!r} not in allowlist"
                        )
                continue
            # Non-literal first arg — require explicit audit entry.
            seen_dynamic.add(site)
            if site not in KNOWN_DYNAMIC_CALLERS:
                kind = type(arg).__name__
                bad.append(
                    f"{site} → non-literal first arg ({kind}); register "
                    f"in KNOWN_DYNAMIC_CALLERS with an audit note OR "
                    f"rewrite as a literal"
                )
    results.append(
        ("every broadcast_global call site is verified",
         not bad,
         "\n  ".join(bad) if bad else ""))

    # 3) Dead-entry detection: every registered FSTRING_EXPANSIONS /
    # KNOWN_DYNAMIC_CALLERS key MUST correspond to a real call site.
    # Catches developer leaving stale audit entries after deleting a
    # call.
    fstring_keys = set(FSTRING_EXPANSIONS.keys())
    dynamic_keys = set(KNOWN_DYNAMIC_CALLERS.keys())
    fstring_stale = fstring_keys - seen_fstrings
    dynamic_stale = dynamic_keys - seen_dynamic
    results.append(
        ("no stale FSTRING_EXPANSIONS entries",
         not fstring_stale, f"stale: {sorted(fstring_stale)}"))
    results.append(
        ("no stale KNOWN_DYNAMIC_CALLERS entries",
         not dynamic_stale, f"stale: {sorted(dynamic_stale)}"))

    # 4) SessionWSBroadcaster.on_change builds `{"type": <literal>}`
    # payloads that _dispatch fans out via broadcast_global. The
    # `session_ws_broadcaster.py` _dispatch entry in KNOWN_DYNAMIC_CALLERS
    # trusts those literals are allowlisted — verify it for real so a new
    # mapping (e.g. session_marker_changed, message_auto_retry_changed)
    # can't slip through silently. Only dict-literal `"type":` keys count;
    # f-strings / comments are ignored.
    bcast = Path(_BACKEND) / "session_ws_broadcaster.py"
    bsrc = bcast.read_text()
    btree = ast.parse(bsrc, filename=str(bcast))
    bcast_types: set[str] = set()
    for node in ast.walk(btree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and isinstance(k.value, str)
                    and k.value == "type"
                    and isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                ):
                    bcast_types.add(v.value)
    bcast_missing = sorted(t for t in bcast_types if t not in ALLOWLIST)
    results.append(
        ("every SessionWSBroadcaster `type` literal is allowlisted",
         not bcast_missing,
         f"missing: {bcast_missing}"))

    frontend_types = Path(_BACKEND).parent / "frontend" / "src" / "types.ts"
    frontend_source = frontend_types.read_text()
    union_start = frontend_source.index("export type WSEventType =")
    union_end = frontend_source.index("export interface WSEvent", union_start)
    frontend_wire_types = set(re.findall(
        r'\|\s*"([^"]+)"',
        frontend_source[union_start:union_end],
    ))
    frontend_missing = sorted(bcast_types - frontend_wire_types)
    results.append(
        ("every SessionWSBroadcaster type is frontend-compatible",
         not frontend_missing,
         f"missing: {frontend_missing}"))

    # 5) Specific sanity: the two types added this session are present.
    results.append(
        ("`models_catalog_changed` in allowlist",
         "models_catalog_changed" in ALLOWLIST, "missing"))
    results.append(
        ("`project_updates_changed` in allowlist",
         "project_updates_changed" in ALLOWLIST, "missing"))
    results.append(
        ("`session_organization_changed` in allowlist",
         "session_organization_changed" in ALLOWLIST, "missing"))
    # PATCH /api/user-prefs broadcasts this after every pref write; an
    # unallowlisted type made the call raise ValueError → 500 on every
    # folder-view / sort / tabs toggle.
    results.append(
        ("`user_prefs_changed` in allowlist",
         "user_prefs_changed" in ALLOWLIST, "missing"))
    results.append(
        ("`session_marker_changed` in allowlist",
         "session_marker_changed" in ALLOWLIST, "missing"))
    results.append(
        ("`session_user_input_changed` in allowlist",
         "session_user_input_changed" in ALLOWLIST, "missing"))
    results.append(
        ("`message_auto_retry_changed` in allowlist",
         "message_auto_retry_changed" in ALLOWLIST, "missing"))
    results.append(
        ("`message_content_updated` in allowlist",
         "message_content_updated" in ALLOWLIST, "missing"))
    results.append(
        ("`message_continuation_changed` in allowlist",
         "message_continuation_changed" in ALLOWLIST, "missing"))
    results.append(
        ("`message_run_meta_changed` in allowlist",
         "message_run_meta_changed" in ALLOWLIST, "missing"))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        import shutil
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

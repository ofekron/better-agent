"""Integration tests for multi-machine support.

Tests fall into two bands:

  - WIRE tests (default, fast): exercise topology validation, handshake,
    schema migrations, shadow-jsonl semantics, provider_for_session
    routing logic. No real `claude` subprocess. Run unconditionally.

  - E2E test (opt-in, slow): spins up primary + node uvicorns in
    separate processes (so env vars + topology are realistic), drives a
    real worker through the delegate flow, asserts events arrive on
    primary. Requires `claude` CLI installed. Enable with
    `BETTER_CLAUDE_E2E=1`.

Run:
    cd backend && .venv/bin/python scripts/integration_test_multi_machine.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _extension_test_helpers import install_machine_nodes_extension  # noqa: E402

# Each test isolates via BETTER_CLAUDE_HOME; drop any inherited BETTER_AGENT_HOME
# (which takes precedence) so a real home can't shadow the per-test tempdir.
os.environ.pop("BETTER_AGENT_HOME", None)


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


# ==========================================================================
# Topology / schema validation tests (synchronous; no servers).
# ==========================================================================

def test_topology_schema_mismatch_raises() -> bool:
    label = "topology.yaml schema_version mismatch raises"
    try:
        tmpdir = tempfile.mkdtemp(prefix="bc-topo-")
        try:
            path = Path(tmpdir) / "topology.yaml"
            path.write_text(
                "schema_version: 99\n"
                "primary: {id: primary, address: 'ws://localhost:8001', cwd_roots: []}\n"
                "nodes: {}\n"
            )
            os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(path)
            import topology
            topology.load_topology(force_reload=True)
            _fail(label, "expected TopologyError, got success")
            return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        if "schema_version" in str(e) and "99" in str(e):
            _ok(label)
            return True
        _fail(label, f"wrong error: {e}")
        return False


def test_topology_missing_env_raises() -> bool:
    label = "topology load with missing env var raises"
    saved = os.environ.pop("BETTER_CLAUDE_TOPOLOGY_PATH", None)
    try:
        import topology
        topology._cache = None
        topology.load_topology(force_reload=True)
        _fail(label, "expected raise, got success")
        return False
    except Exception as e:
        if "BETTER_CLAUDE_TOPOLOGY_PATH" in str(e):
            _ok(label)
            return True
        _fail(label, f"wrong error: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = saved


def test_topology_local_node_id_unknown_raises() -> bool:
    label = "local_node_id() unknown node raises"
    tmpdir = tempfile.mkdtemp(prefix="bc-topo-")
    try:
        path = Path(tmpdir) / "topology.yaml"
        path.write_text(
            "schema_version: 1\n"
            "primary: {id: primary, address: 'ws://localhost:8001', cwd_roots: []}\n"
            "nodes: {}\n"
        )
        os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(path)
        os.environ["BETTER_CLAUDE_NODE_ID"] = "ghost"
        import topology
        topology._cache = None
        topology.local_node_id()
        _fail(label, "expected raise")
        return False
    except Exception as e:
        if "ghost" in str(e):
            _ok(label)
            return True
        _fail(label, f"wrong error: {e}")
        return False
    finally:
        os.environ.pop("BETTER_CLAUDE_NODE_ID", None)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_resolve_known_spec_per_node_auth() -> bool:
    """Model C: every node authenticates with its own per-node registry
    secret (argon2). topology.yaml is an allowlist of permitted ids +
    each declared node's cwd_roots policy; there is NO shared token.
    Directly exercises node_link._resolve_known_spec (no server/WS)."""
    label = "_resolve_known_spec enforces per-node auth + topology allowlist"
    home = tempfile.mkdtemp(prefix="bc-auth-")
    topo_path = Path(home) / "topology.yaml"
    topo_path.write_text(
        "schema_version: 1\n"
        "primary: {id: primary, address: 'ws://localhost:8001', cwd_roots: []}\n"
        "nodes:\n"
        "  declared: {address: 'ws://localhost:8002', cwd_roots: ['/safe']}\n"
    )
    saved_home = os.environ.get("BETTER_AGENT_HOME") or os.environ.get("BETTER_CLAUDE_HOME")
    saved_topo = os.environ.get("BETTER_AGENT_TOPOLOGY_PATH") or os.environ.get("BETTER_CLAUDE_TOPOLOGY_PATH")
    # get_env prefers BETTER_AGENT_* over BETTER_CLAUDE_*, so set BOTH
    # prefixes or the dev shell's real topology/home shadows the fixture.
    os.environ["BETTER_AGENT_HOME"] = home
    os.environ["BETTER_CLAUDE_HOME"] = home
    os.environ["BETTER_AGENT_TOPOLOGY_PATH"] = str(topo_path)
    os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(topo_path)
    try:
        import topology
        import node_registry_store
        import node_link
        topology._cache = None
        node_registry_store.add(
            node_id="declared", address="", cwd_roots=["/self-reported"],
            secret_hash=node_registry_store.hash_secret("s-declared"),
        )
        node_registry_store.add(
            node_id="dynamic", address="", cwd_roots=["/dyn"],
            secret_hash=node_registry_store.hash_secret("s-dynamic"),
        )

        # 1. Declared + approved + correct secret → topology spec wins
        #    (manifest cwd_roots, NOT the self-reported /self-reported).
        spec, reason = node_link._resolve_known_spec("declared", "s-declared")
        if reason is not None or spec is None or spec.cwd_roots != ("/safe",):
            _fail(label, f"declared good secret: spec={spec!r} reason={reason!r}")
            return False
        # 2. Declared + approved + WRONG secret → bad secret.
        spec, reason = node_link._resolve_known_spec("declared", "wrong")
        if spec is not None or reason != "bad secret":
            _fail(label, f"declared bad secret: spec={spec!r} reason={reason!r}")
            return False
        # 3. Declared but NOT yet approved → registration flow (None, None).
        node_registry_store.remove("declared")
        spec, reason = node_link._resolve_known_spec("declared", "whatever")
        if not (spec is None and reason is None):
            _fail(label, f"declared unapproved must be (None,None): {spec!r} {reason!r}")
            return False
        # 4. Topology allowlist: unknown id, not approved → hard reject.
        spec, reason = node_link._resolve_known_spec("ghost", "x")
        if spec is not None or reason is None or "topology" not in reason:
            _fail(label, f"ghost must be allowlist-rejected: {spec!r} {reason!r}")
            return False
        # 5. Dynamic (not in topology) but approved → registry spec.
        spec, reason = node_link._resolve_known_spec("dynamic", "s-dynamic")
        if reason is not None or spec is None or spec.cwd_roots != ("/dyn",):
            _fail(label, f"dynamic approved: spec={spec!r} reason={reason!r}")
            return False
        # 6. Dynamic + wrong secret → bad secret.
        spec, reason = node_link._resolve_known_spec("dynamic", "nope")
        if spec is not None or reason != "bad secret":
            _fail(label, f"dynamic bad secret: {spec!r} {reason!r}")
            return False
        _ok(label)
        return True
    finally:
        for prefix in ("BETTER_AGENT_", "BETTER_CLAUDE_"):
            if saved_home is not None:
                os.environ[f"{prefix}HOME"] = saved_home
            else:
                os.environ.pop(f"{prefix}HOME", None)
            if saved_topo is not None:
                os.environ[f"{prefix}TOPOLOGY_PATH"] = saved_topo
            else:
                os.environ.pop(f"{prefix}TOPOLOGY_PATH", None)
        shutil.rmtree(home, ignore_errors=True)


# ==========================================================================
# Schema migration tests
# ==========================================================================

def test_session_store_v7_default_node_id() -> bool:
    label = "session_store v7 setdefault node_id='primary'"
    home = tempfile.mkdtemp(prefix="bc-sst-")
    saved = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        # Reimport session_store under the new home.
        import importlib
        import paths
        import session_store
        importlib.reload(paths)
        importlib.reload(session_store)
        sess = session_store.create_session(name="x", cwd="/tmp")
        if sess.get("node_id") != "primary":
            _fail(label, f"expected primary, got {sess.get('node_id')!r}")
            return False
        # Now simulate a v6 file (no node_id field) and re-read.
        v6 = {**sess, "_schema_version": 6}
        v6.pop("node_id", None)
        (Path(home) / "sessions" / f"{v6['id']}.json").write_text(json.dumps(v6))
        migrated = session_store.get_session(v6["id"])
        if migrated.get("node_id") != "primary":
            _fail(label, f"v6 migration didn't set node_id: {migrated!r}")
            return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


def test_worker_store_v5_default_node_id() -> bool:
    label = "worker_store v5 setdefault node_id='primary'"
    home = tempfile.mkdtemp(prefix="bc-ws-")
    saved = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        import importlib
        import paths
        from stores import worker_store
        importlib.reload(paths)
        importlib.reload(worker_store)
        record = worker_store.upsert_worker(
            cwd="/tmp/test-ws",
            agent_session_id="bc-x",
            orchestration_mode="native",
            agent_sid="ag-x",
            node_id="linux-box",
        )
        if record.get("node_id") != "linux-box":
            _fail(label, f"upsert didn't store node_id: {record!r}")
            return False
        # Simulate v4 file on disk.
        v4 = {
            "version": 4,
            "workers": [{
                "agent_session_id": "bc-y", "orchestration_mode": "native",
                "agent_sid": "ag-y", "created_at": "", "last_active": "",
                "delegation_count": 0, "token_usage": {},
            }],
            "forks": {},
        }
        path = worker_store._path_for("/tmp/test-ws-legacy")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(v4))
        # Read → should set node_id="primary".
        result = worker_store.get_worker("/tmp/test-ws-legacy", "bc-y")
        if result.get("node_id") != "primary":
            _fail(label, f"v4 migration didn't set primary: {result!r}")
            return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


def test_pending_approvals_node_id_field() -> bool:
    label = "pending_approvals carries node_id"
    home = tempfile.mkdtemp(prefix="bc-pa-")
    saved = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        import importlib
        import paths
        from stores import pending_approvals
        importlib.reload(paths)
        importlib.reload(pending_approvals)
        rec = pending_approvals.create(
            delegation_id="d1",
            app_session_id="s1",
            cwd="/tmp",
            justification="j",
            proposed_description="d",
            proposed_orchestration_mode="native",
            instructions_preview="i",
            model="m",
            node_id="linux-box",
        )
        if rec.get("node_id") != "linux-box":
            _fail(label, f"node_id not on record: {rec!r}")
            return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


# ==========================================================================
# dispatch_rpc + project_store unit tests (no uvicorn)
# ==========================================================================

async def test_dispatch_rpc_json_serializability() -> bool:
    """For each FS handler, the return value MUST be json-serializable
    so the wire path (`ws.send_json`) doesn't choke. Locks against a
    future regression where a handler returns bytes/datetime/Path."""
    label = "dispatch_rpc handlers return json-serializable dicts"
    import importlib
    tmp_root = tempfile.mkdtemp(prefix="bc-rpc-")
    tmp_file = Path(tmp_root) / "hello world.txt"
    tmp_file.write_text("baseline content")
    # Subdir with spaces — locks the loosened _SAFE_PATH_RE regression.
    saved_topo = os.environ.pop("BETTER_CLAUDE_TOPOLOGY_PATH", None)
    try:
        import topology
        topology._cache = None
        import node_rpc_handlers
        importlib.reload(node_rpc_handlers)
        cases = [
            ("list_dir", {"path": tmp_root}),
            ("list_directories", {"path": tmp_root}),
            ("get_file_tree", {"root": tmp_root}),
            ("search_tree", {"root": tmp_root, "query": "hello", "kind": "file"}),
            ("get_file_content", {"path": str(tmp_file)}),
            ("write_file_content", {"path": str(tmp_file), "content": "new"}),
            ("reconstruct_before_edit", {"file_path": str(tmp_file), "old_string": "a", "new_string": "b"}),
            ("get_git_status", {"cwd": tmp_root}),
            ("get_file_diff", {"file_path": str(tmp_file), "cwd": tmp_root}),
            ("scan_project_configs", {"cwd": tmp_root}),
            ("file_editor_baseline", {"file_path": str(tmp_file), "cwd": tmp_root}),
        ]
        for method, params in cases:
            try:
                result = await node_rpc_handlers.dispatch_rpc(method, params)
            except Exception as e:
                _fail(label, f"{method} raised: {e}")
                return False
            try:
                json.dumps(result)
            except (TypeError, ValueError) as e:
                _fail(label, f"{method} returned non-JSON value: {e}")
                return False
        _ok(label)
        return True
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        if saved_topo is not None:
            os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = saved_topo


async def test_dispatch_rpc_rejects_path_outside_cwd_roots() -> bool:
    """When a topology declares the local node with non-empty cwd_roots,
    dispatch_rpc handlers MUST refuse paths outside the allowlist."""
    label = "dispatch_rpc rejects path outside cwd_roots (defense in depth)"
    home = tempfile.mkdtemp(prefix="bc-cwd-")
    saved_home = os.environ.get("BETTER_CLAUDE_HOME")
    saved_topo = os.environ.get("BETTER_CLAUDE_TOPOLOGY_PATH")
    os.environ["BETTER_CLAUDE_HOME"] = home
    topo = Path(home) / "topology.yaml"
    topo.write_text(
        "schema_version: 1\n"
        "primary: {id: primary, address: 'ws://localhost:9999', cwd_roots: ['/tmp']}\n"
        "nodes: {}\n"
    )
    os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(topo)
    try:
        import importlib
        import topology
        topology._cache = None
        import node_rpc_handlers
        importlib.reload(node_rpc_handlers)
        try:
            await node_rpc_handlers.dispatch_rpc(
                "get_file_content", {"path": "/etc/hostname"},
            )
            _fail(label, "expected ValueError for path outside /tmp")
            return False
        except ValueError as e:
            if "cwd_roots" not in str(e):
                _fail(label, f"wrong error: {e}")
                return False
        # Path inside cwd_roots should still work (proves the guard
        # isn't blanket-blocking).
        tmp_file = Path("/tmp") / f"bc-test-{uuid.uuid4().hex[:8]}.txt"
        tmp_file.write_text("ok")
        try:
            result = await node_rpc_handlers.dispatch_rpc(
                "get_file_content", {"path": str(tmp_file)},
            )
            if result.get("content") != "ok":
                _fail(label, f"in-allowlist read returned {result!r}")
                return False
        finally:
            tmp_file.unlink(missing_ok=True)
        _ok(label)
        return True
    finally:
        if saved_home is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved_home
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        if saved_topo is not None:
            os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = saved_topo
        else:
            os.environ.pop("BETTER_CLAUDE_TOPOLOGY_PATH", None)
        shutil.rmtree(home, ignore_errors=True)


async def test_run_headless_rewind_rpc_wiring() -> bool:
    """run_headless/rewind are async request/response RPCs over the same
    `_HANDLERS` table as the fs handlers. Locks: they're registered, they
    are coroutine functions (so dispatch_rpc awaits them on-loop rather
    than running them off-loop via to_thread), rewind validates its
    params before touching the provider, and the unknown-method path
    still rejects. Does NOT spawn claude — the real two-node round-trip
    is the heavy harness's job."""
    label = "run_headless/rewind registered as async rpc handlers"
    import inspect
    import importlib
    import node_rpc_handlers
    importlib.reload(node_rpc_handlers)
    h = node_rpc_handlers._HANDLERS
    for method in ("run_headless", "rewind"):
        if method not in h:
            _fail(label, f"{method!r} not in _HANDLERS")
            return False
        if not inspect.iscoroutinefunction(h[method]):
            _fail(label, f"{method!r} handler must be async")
            return False
    try:
        await node_rpc_handlers.dispatch_rpc("rewind", {})
    except ValueError:
        pass
    except Exception as e:  # noqa: BLE001
        _fail(label, f"rewind({{}}) raised non-ValueError: {type(e).__name__}")
        return False
    else:
        _fail(label, "rewind({}) should require agent_sid + message_uuid")
        return False
    try:
        await node_rpc_handlers.dispatch_rpc("totally_unknown", {})
    except ValueError:
        pass
    except Exception as e:  # noqa: BLE001
        _fail(label, f"unknown method raised non-ValueError: {type(e).__name__}")
        return False
    else:
        _fail(label, "unknown method should be rejected")
        return False
    _ok(label)
    return True


async def test_provisioning_node_id_routing() -> bool:
    """A provisioned session configured for a remote node MUST stamp
    node_id on the base+caller sessions and refuse to silently reuse a
    base on a different node. Locks the wiring that lets a provisioned
    fork run on a remote node via RemoteProviderProxy (routing is keyed
    off the base session's node_id at dispatch time)."""
    label = "provisioning threads node_id into sessions + rejects mismatch"
    home = tempfile.mkdtemp(prefix="bc-prov-")
    saved_home = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    os.environ["T_NODE_ID"] = "worker-1"
    try:
        import importlib
        import paths
        importlib.reload(paths)
        from provisioning import spec as spec_mod
        from provisioning.config import resolve_config
        from provisioning.lifecycle import _validate_provider

        class _Spec(spec_mod.ProvisionedSessionSpec):
            key = "t"
            version = 1
            name = "T"
            env_prefix = "T"
            task_key = ""
            def build_provision_prompt(self, ctx):
                return "p"

        cfg = resolve_config(_Spec())
        if cfg.node_id != "worker-1":
            _fail(label, f"config node_id={cfg.node_id!r}, expected worker-1")
            return False

        # node_id mismatch must raise — never silently route to the
        # wrong node.
        try:
            _validate_provider(
                {"id": "s1", "provider_id": cfg.provider_id,
                 "model": cfg.model, "node_id": "primary"},
                cfg,
            )
            _fail(label, "_validate_provider accepted a node_id mismatch")
            return False
        except RuntimeError as e:
            if "node_id mismatch" not in str(e):
                _fail(label, f"wrong error: {e}")
                return False

        # Full lifecycle: ensure_session creates a base carrying node_id
        # and a second call reuses it (node-aware lookup).
        from provisioning.lifecycle import ensure_session
        base1 = ensure_session(_Spec(), cfg)
        import session_manager as sm
        sm_mod = sm.manager if hasattr(sm, "manager") else sm
        rec = sm_mod.get(base1) or {}
        if (rec.get("node_id") or "primary") != "worker-1":
            _fail(label, f"base session node_id={rec.get('node_id')!r}")
            return False
        base2 = ensure_session(_Spec(), cfg)
        if base2 != base1:
            _fail(label, f"node-aware reuse failed: {base1} vs {base2}")
            return False
        _ok(label)
        return True
    finally:
        os.environ.pop("T_NODE_ID", None)
        if saved_home is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved_home
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


def test_project_store_v2_round_trip() -> bool:
    label = "project_store v2 add+list round-trip with node_id"
    home = tempfile.mkdtemp(prefix="bc-ps-")
    saved_agent = os.environ.get("BETTER_AGENT_HOME")
    saved_claude = os.environ.pop("BETTER_CLAUDE_HOME", None)
    os.environ["BETTER_AGENT_HOME"] = home
    try:
        import importlib
        import paths
        import project_store
        importlib.reload(paths)
        importlib.reload(project_store)
        # On macOS, /tmp resolves to /private/tmp. Use whatever
        # `_normalize` produces as the source of truth.
        resolved_tmp = str(Path("/tmp").resolve())
        r1 = project_store.add_project("/tmp", name="local-tmp", node_id="primary")
        r2 = project_store.add_project("/tmp", name="remote-tmp", node_id="linux-box")
        if not r1 or r1.get("node_id") != "primary":
            _fail(label, f"add primary returned {r1!r}")
            return False
        if not r2 or r2.get("node_id") != "linux-box":
            _fail(label, f"add remote returned {r2!r}")
            return False
        all_projects = project_store.list_projects()
        ids = {(p["path"], p["node_id"]) for p in all_projects}
        if (resolved_tmp, "primary") not in ids or (resolved_tmp, "linux-box") not in ids:
            _fail(label, f"missing rows: {all_projects!r}")
            return False
        _ok(label)
        return True
    finally:
        if saved_agent is not None:
            os.environ["BETTER_AGENT_HOME"] = saved_agent
        else:
            os.environ.pop("BETTER_AGENT_HOME", None)
        if saved_claude is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved_claude
        shutil.rmtree(home, ignore_errors=True)


def test_project_store_v1_migrates_and_backs_up() -> bool:
    label = "project_store v1 schema → migrate + backup"
    home = tempfile.mkdtemp(prefix="bc-ps-v1-")
    saved_agent = os.environ.get("BETTER_AGENT_HOME")
    saved_claude = os.environ.pop("BETTER_CLAUDE_HOME", None)
    os.environ["BETTER_AGENT_HOME"] = home
    try:
        import importlib
        import paths
        import project_store
        importlib.reload(paths)
        importlib.reload(project_store)
        project_store.ensure_git_remote = lambda _path: None
        # Write a v1 (bare list) projects.json directly.
        v1_path = Path(home) / "projects.json"
        v1_path.write_text(json.dumps([
            {"path": "/tmp", "name": "old", "created_at": "x", "last_used": "y"},
            {"path": "", "name": "invalid"},
        ]))
        migrated = project_store.list_projects()
        resolved_tmp = str(Path("/tmp").resolve())
        if len(migrated) != 1:
            _fail(label, f"expected one migrated project, got {migrated!r}")
            return False
        row = migrated[0]
        if row.get("path") != resolved_tmp or row.get("node_id") != "primary":
            _fail(label, f"wrong migrated row: {row!r}")
            return False
        if row.get("name") != "old" or row.get("created_at") != "x" or row.get("last_used") != "y":
            _fail(label, f"did not preserve v1 fields: {row!r}")
            return False
        bak = Path(home) / "projects.v1.bak.json"
        if not bak.exists():
            _fail(label, "backup file not created")
            return False
        stored = json.loads(v1_path.read_text())
        if stored.get("version") != 2 or len(stored.get("projects") or []) != 1:
            _fail(label, f"projects.json not rewritten as v2: {stored!r}")
            return False
        if (Path(home) / "projects.v1.repaired").exists():
            _fail(label, "repair marker should no longer be written")
            return False
        _ok(label)
        return True
    finally:
        if saved_agent is not None:
            os.environ["BETTER_AGENT_HOME"] = saved_agent
        else:
            os.environ.pop("BETTER_AGENT_HOME", None)
        if saved_claude is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved_claude
        shutil.rmtree(home, ignore_errors=True)


def test_project_store_repairs_partial_v2_from_v1_backup() -> bool:
    label = "project_store partial v2 + v1 backup → idempotent repair"
    home = tempfile.mkdtemp(prefix="bc-ps-repair-")
    saved_agent = os.environ.get("BETTER_AGENT_HOME")
    saved_claude = os.environ.pop("BETTER_CLAUDE_HOME", None)
    os.environ["BETTER_AGENT_HOME"] = home
    try:
        import importlib
        import paths
        import project_store
        importlib.reload(paths)
        importlib.reload(project_store)
        project_store.ensure_git_remote = lambda _path: None
        resolved_tmp = str(Path("/tmp").resolve())
        resolved_home = str(Path.home().resolve())
        path = Path(home) / "projects.json"
        path.write_text(json.dumps({
            "version": 2,
            "projects": [{
                "path": resolved_tmp,
                "node_id": "primary",
                "name": "current",
                "created_at": "new",
                "last_used": "new",
            }],
        }))
        (Path(home) / "projects.v1.bak.json").write_text(json.dumps([
            {"path": "/tmp", "name": "old"},
            {"path": str(Path.home()), "name": "home", "created_at": "a", "last_used": "b"},
        ]))
        repaired = project_store.list_projects()
        ids = {(p.get("node_id"), p.get("path")) for p in repaired}
        if ids != {("primary", resolved_tmp), ("primary", resolved_home)}:
            _fail(label, f"wrong repaired ids: {repaired!r}")
            return False
        home_rows = [p for p in repaired if p.get("path") == resolved_home]
        if len(home_rows) != 1 or home_rows[0].get("name") != "home":
            _fail(label, f"missing restored backup row: {repaired!r}")
            return False
        if (Path(home) / "projects.v1.repaired").exists():
            _fail(label, "repair marker should no longer be written")
            return False
        project_store.remove_project(resolved_home, node_id="primary")
        after_delete = project_store.list_projects()
        if any(p.get("path") == resolved_home for p in after_delete):
            _fail(label, f"deleted backup row resurrected: {after_delete!r}")
            return False
        _ok(label)
        return True
    finally:
        if saved_agent is not None:
            os.environ["BETTER_AGENT_HOME"] = saved_agent
        else:
            os.environ.pop("BETTER_AGENT_HOME", None)
        if saved_claude is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved_claude
        shutil.rmtree(home, ignore_errors=True)


def test_project_store_repairs_despite_stale_marker() -> bool:
    """Regression: repair used to be gated by a one-shot marker file.
    Once that marker was written (before an earlier migration loss had
    healed), recovery was permanently disabled and real projects (e.g.
    testape) stayed missing forever. Repair must now ignore any stale
    marker and restore missing v2 rows from the v1 backup idempotently."""
    label = "project_store repairs missing rows despite a stale repair marker"
    home = tempfile.mkdtemp(prefix="bc-ps-stale-")
    saved_agent = os.environ.get("BETTER_AGENT_HOME")
    saved_claude = os.environ.pop("BETTER_CLAUDE_HOME", None)
    os.environ["BETTER_AGENT_HOME"] = home
    try:
        import importlib
        import paths
        import project_store
        importlib.reload(paths)
        importlib.reload(project_store)
        project_store.ensure_git_remote = lambda _path: None
        resolved_tmp = str(Path("/tmp").resolve())
        resolved_home = str(Path.home().resolve())
        (Path(home) / "projects.json").write_text(json.dumps({
            "version": 2,
            "projects": [{
                "path": resolved_tmp,
                "node_id": "primary",
                "name": "current",
                "created_at": "n",
                "last_used": "n",
            }],
        }))
        (Path(home) / "projects.v1.bak.json").write_text(json.dumps([
            {"path": "/tmp"},
            {"path": str(Path.home())},
        ]))
        # Stale marker left over from the old one-shot repair scheme.
        (Path(home) / "projects.v1.repaired").write_text("stale")
        repaired = project_store.list_projects()
        paths_after = {p.get("path") for p in repaired}
        if resolved_home not in paths_after:
            _fail(label, f"stale marker blocked restore of missing row: {repaired!r}")
            return False
        # Idempotent: re-reading must neither duplicate nor drop rows.
        again = project_store.list_projects()
        if {p.get("path") for p in again} != paths_after:
            _fail(label, f"non-idempotent re-read: {again!r}")
            return False
        _ok(label)
        return True
    finally:
        if saved_agent is not None:
            os.environ["BETTER_AGENT_HOME"] = saved_agent
        else:
            os.environ.pop("BETTER_AGENT_HOME", None)
        if saved_claude is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved_claude
        shutil.rmtree(home, ignore_errors=True)


async def test_file_op_no_topology_routes_remote_not_local() -> bool:
    """Without topology.yaml, a non-primary node_id MUST route to
    `node_link.rpc_call` (raising `NodeOffline` when the node isn't
    connected) — never silently fall back to local `dispatch_rpc`.
    Dynamic worker nodes register without topology and are reachable
    only via node_link, so aborting with a topology error broke
    browsing them."""
    label = "call_local_or_remote without topology routes remote, not local"
    saved = os.environ.pop("BETTER_CLAUDE_TOPOLOGY_PATH", None)
    local_dispatched = {"hit": False}

    def _local_trap(*_a, **_kw):
        local_dispatched["hit"] = True
        return {}

    try:
        import importlib
        import topology
        topology._cache = None
        import node_rpc_handlers
        importlib.reload(node_rpc_handlers)
        node_rpc_handlers.dispatch_rpc = _local_trap  # guard: no local fallback
        try:
            await node_rpc_handlers.call_local_or_remote(
                "linux-box", "list_directories", {"path": "/tmp"},
            )
            _fail(label, "expected NodeOffline, got success")
            return False
        except Exception as e:
            import node_link
            if not isinstance(e, node_link.NodeOffline):
                _fail(label, f"expected NodeOffline, got {type(e).__name__}: {e}")
                return False
        if local_dispatched["hit"]:
            _fail(label, "dispatched locally on a non-primary node_id")
            return False
        _ok(label)
        return True
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = saved


async def test_file_op_no_topology_routes_connected_dynamic_node() -> bool:
    """The fix's whole point: without topology.yaml, a CONNECTED
    dynamic node is served via node_link and the op succeeds. Stubs
    `node_link.rpc_call` to a canned reply so we don't need a live
    WS connection; asserts the call reaches node_link with the right
    node_id and never touches local dispatch."""
    label = "call_local_or_remote serves connected dynamic node via node_link"
    saved = os.environ.pop("BETTER_CLAUDE_TOPOLOGY_PATH", None)
    seen = {"node_id": None}
    local_dispatched = {"hit": False}

    def _local_trap(*_a, **_kw):
        local_dispatched["hit"] = True
        return {}

    try:
        import importlib
        import topology
        topology._cache = None
        import node_rpc_handlers
        importlib.reload(node_rpc_handlers)
        node_rpc_handlers.dispatch_rpc = _local_trap
        import node_link
        orig_rpc = node_link.rpc_call

        async def _fake_rpc(node_id, method, params, *_, **__):
            seen["node_id"] = node_id
            return {"entries": [], "path": params.get("path", "")}

        node_link.rpc_call = _fake_rpc
        try:
            result = await node_rpc_handlers.call_local_or_remote(
                "ofeks-macbook-air", "list_directories", {"path": "/home"},
            )
        finally:
            node_link.rpc_call = orig_rpc
        if seen["node_id"] != "ofeks-macbook-air":
            _fail(label, f"node_link not called with right id: {seen['node_id']}")
            return False
        if local_dispatched["hit"]:
            _fail(label, "dispatched locally instead of via node_link")
            return False
        if result != {"entries": [], "path": "/home"}:
            _fail(label, f"unexpected result: {result}")
            return False
        _ok(label)
        return True
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = saved


# ==========================================================================
# shadow_jsonl tests
# ==========================================================================

async def test_shadow_append_simple() -> bool:
    label = "shadow_jsonl append in-order"
    home = tempfile.mkdtemp(prefix="bc-sh-")
    saved = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        import importlib
        import paths
        import shadow_jsonl
        importlib.reload(paths)
        importlib.reload(shadow_jsonl)
        shadow_jsonl.reset_for_tests()
        rid, sid = "root1", "sid1"
        off = 0
        for line in ["alpha", "beta", "gamma"]:
            await shadow_jsonl.append(
                node_id="n1", root_id=rid, fork_agent_sid=sid,
                file_version=1, line_offset_in_version=off, line=line,
            )
            off += len(line) + 1
        path = shadow_jsonl.shadow_path(rid, sid)
        got = path.read_text().splitlines()
        if got != ["alpha", "beta", "gamma"]:
            _fail(label, f"got {got!r}")
            return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


async def test_shadow_partial_line_recovery() -> bool:
    label = "shadow_jsonl truncate-and-rewrite on retransmit"
    home = tempfile.mkdtemp(prefix="bc-sh-")
    saved = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        import importlib
        import paths
        import shadow_jsonl
        importlib.reload(paths)
        importlib.reload(shadow_jsonl)
        shadow_jsonl.reset_for_tests()
        rid, sid = "root2", "sid2"
        # First line at offset 0.
        await shadow_jsonl.append(
            node_id="n", root_id=rid, fork_agent_sid=sid,
            file_version=1, line_offset_in_version=0, line="alpha",
        )
        # Retransmit same line — should truncate and rewrite, leaving
        # one line on disk.
        await shadow_jsonl.append(
            node_id="n", root_id=rid, fork_agent_sid=sid,
            file_version=1, line_offset_in_version=0, line="alpha-fixed",
        )
        got = shadow_jsonl.shadow_path(rid, sid).read_text().splitlines()
        if got != ["alpha-fixed"]:
            _fail(label, f"got {got!r}")
            return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


async def test_shadow_version_bump_truncates() -> bool:
    label = "shadow_jsonl file_version bump truncates"
    home = tempfile.mkdtemp(prefix="bc-sh-")
    saved = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        import importlib
        import paths
        import shadow_jsonl
        importlib.reload(paths)
        importlib.reload(shadow_jsonl)
        shadow_jsonl.reset_for_tests()
        rid, sid = "root3", "sid3"
        await shadow_jsonl.append(
            node_id="n", root_id=rid, fork_agent_sid=sid,
            file_version=1, line_offset_in_version=0, line="v1-line1",
        )
        await shadow_jsonl.append(
            node_id="n", root_id=rid, fork_agent_sid=sid,
            file_version=1, line_offset_in_version=9, line="v1-line2",
        )
        # Bump to v2 — simulate claude compaction.
        await shadow_jsonl.append(
            node_id="n", root_id=rid, fork_agent_sid=sid,
            file_version=2, line_offset_in_version=0, line="v2-line1",
        )
        got = shadow_jsonl.shadow_path(rid, sid).read_text().splitlines()
        if got != ["v2-line1"]:
            _fail(label, f"got {got!r}")
            return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


async def test_shadow_concurrent_writes_serialize() -> bool:
    label = "shadow_jsonl concurrent writes serialize"
    home = tempfile.mkdtemp(prefix="bc-sh-")
    saved = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        import importlib
        import paths
        import shadow_jsonl
        importlib.reload(paths)
        importlib.reload(shadow_jsonl)
        shadow_jsonl.reset_for_tests()
        rid, sid = "root4", "sid4"
        # Three concurrent writers at non-overlapping offsets.
        async def w(line, off):
            await shadow_jsonl.append(
                node_id="n", root_id=rid, fork_agent_sid=sid,
                file_version=1, line_offset_in_version=off, line=line,
            )
        # Submit out-of-order; per-file lock should serialize.
        await asyncio.gather(
            w("alpha", 0),
            w("beta", 6),
            w("gamma", 11),
        )
        text = shadow_jsonl.shadow_path(rid, sid).read_text()
        # Lines should not be interleaved/torn. The exact order isn't
        # guaranteed by asyncio.gather, but EACH line must be intact.
        for needed in ["alpha", "beta", "gamma"]:
            if needed + "\n" not in text:
                _fail(label, f"line {needed!r} not intact: {text!r}")
                return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


# ==========================================================================
# provider_for_session routing tests
# ==========================================================================

def test_provider_for_session_local_unchanged() -> bool:
    label = "provider_for_session returns local for node_id='primary'"
    home = tempfile.mkdtemp(prefix="bc-pfs-")
    saved_home = os.environ.get("BETTER_CLAUDE_HOME")
    os.environ["BETTER_CLAUDE_HOME"] = home
    try:
        import importlib
        import paths
        import session_store
        from session_manager import manager as sm
        importlib.reload(paths)
        importlib.reload(session_store)
        sess = sm.create(name="local", cwd="/tmp", orchestration_mode="manager")
        from orchestrator import Coordinator
        coord = Coordinator()
        prov = coord.provider_for_session(sess["id"])
        # It should NOT be a RemoteProviderProxy.
        import provider_remote
        if isinstance(prov, provider_remote.RemoteProviderProxy):
            _fail(label, "returned RemoteProviderProxy for local session")
            return False
        _ok(label)
        return True
    except Exception as e:
        _fail(label, f"unexpected: {e}")
        return False
    finally:
        if saved_home is not None:
            os.environ["BETTER_CLAUDE_HOME"] = saved_home
        else:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


# ==========================================================================
# WS handshake tests (primary side)
# ==========================================================================

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    def __init__(self, app_path: str, port: int, env: dict | None = None):
        self.app_path = app_path
        self.port = port
        self.env = env or {}
        self.server = None
        self.thread = None

    def start(self):
        # Apply env BEFORE uvicorn starts so the imported app picks it up.
        for k, v in self.env.items():
            os.environ[k] = v
        import uvicorn
        cfg = uvicorn.Config(self.app_path, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"uvicorn {self.app_path} failed to start")

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


# Shared backend fixture — single BETTER_CLAUDE_HOME + single uvicorn
# for the handshake + session-create + ws-broadcast tests. main.py's
# logging FileHandler is bound to ba_home() at import time, so we can't
# tear down + recreate the home dir between tests without crashing the
# logger.
async def run_handshake_tests() -> list[bool]:
    import websockets
    import httpx

    home = tempfile.mkdtemp(prefix="bc-h-")
    topo_path = Path(home) / "topology.yaml"
    port = free_port()
    next_port = port + 1
    topo_path.write_text(
        f"schema_version: 1\n"
        f"primary: {{id: primary, address: 'ws://localhost:{port}', cwd_roots: []}}\n"
        f"nodes:\n"
        f"  n1: {{address: 'ws://localhost:{next_port}', cwd_roots: ['/tmp']}}\n"
        f"  n2: {{address: 'ws://localhost:{next_port + 1}', cwd_roots: ['/tmp']}}\n"
    )
    os.environ["BETTER_CLAUDE_HOME"] = home
    os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(topo_path)
    os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
    import topology
    topology._cache = None
    # Per-node-secret auth (Model C): pre-approve n1 with its own argon2
    # secret. There is no shared token; the node presents "good-token" and
    # primary verifies it against this hash via node_registry_store.
    import node_registry_store
    install_machine_nodes_extension(home)
    node_registry_store.add(
        node_id="n1",
        address=f"ws://localhost:{next_port}",
        cwd_roots=["/tmp"],
        secret_hash=node_registry_store.hash_secret("good-token"),
    )
    node_registry_store.add(
        node_id="n2",
        address=f"ws://localhost:{next_port + 1}",
        cwd_roots=["/tmp"],
        secret_hash=node_registry_store.hash_secret("good-token"),
    )

    server = BackgroundUvicorn("main:app", port)
    results: list[bool] = []
    try:
        server.start()
        ws_url = f"ws://127.0.0.1:{port}/api/node/connect"
        ws_url_chat = f"ws://127.0.0.1:{port}/ws/chat"
        base_url = f"http://127.0.0.1:{port}"
        from auth_test_helpers import authenticate_async_client
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as auth_client:
            user_token = await authenticate_async_client(auth_client)
        authed_headers = {"Authorization": f"Bearer {user_token}"}
        ws_url_chat = f"{ws_url_chat}?token={user_token}"

        label = "node_link tolerates disconnect before handshake"
        ok = False
        captured_errors: list[str] = []

        class _CaptureErrors(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_errors.append(self.format(record))

        log_handler = _CaptureErrors()
        log_handler.setLevel(logging.ERROR)
        log_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        logging.getLogger().addHandler(log_handler)
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": "Bearer good-token"},
            ):
                ok = True
        except Exception:
            pass
        finally:
            logging.getLogger().removeHandler(log_handler)
        server_error = "\n".join(captured_errors)
        if ok and ("transfer_data_task" in server_error or "Exception in ASGI application" in server_error):
            ok = False
        if ok:
            _ok(label)
        else:
            _fail(label, "pre-handshake disconnect raised or logged server traceback")
        results.append(ok)

        # --- Bad per-node secret ---
        label = "node_link rejects bad per-node secret"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": "Bearer WRONG"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 1, "node_id": "n1",
                }))
                reply = json.loads(await ws.recv())
                if reply.get("type") == "handshake_reject":
                    ok = True
        except Exception:
            pass
        if ok:
            _ok(label)
        else:
            _fail(label, "bad-secret handshake did not reject")
        results.append(ok)

        # --- Protocol version mismatch ---
        label = "node_link rejects protocol_version mismatch"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": "Bearer good-token"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 99, "node_id": "n1",
                }))
                reply = json.loads(await ws.recv())
                if reply.get("type") == "handshake_reject" and "protocol_version" in (reply.get("reason") or ""):
                    ok = True
        except Exception:
            pass
        if ok:
            _ok(label)
        else:
            _fail(label, "no handshake_reject for v99")
        results.append(ok)

        # --- Unknown node_id (topology allowlist) ---
        label = "node_link rejects node_id not declared in topology"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": "Bearer good-token"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 1, "node_id": "ghost",
                }))
                reply = json.loads(await ws.recv())
                if reply.get("type") == "handshake_reject":
                    ok = True
        except Exception:
            pass
        if ok:
            _ok(label)
        else:
            _fail(label, "no handshake_reject for unknown node")
        results.append(ok)

        # --- Success registers node ---
        label = "valid handshake registers node + GET /api/nodes shows it"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": "Bearer good-token"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 1, "node_id": "n1",
                }))
                reply = json.loads(await ws.recv())
                if reply.get("type") == "handshake":
                    await asyncio.sleep(0.2)
                    async with httpx.AsyncClient(headers=authed_headers) as client:
                        resp = await client.get(f"http://127.0.0.1:{port}/api/nodes")
                        snap = resp.json()
                    n1 = next((n for n in snap if n["id"] == "n1"), None)
                    if n1 and n1["state"] == "connected":
                        ok = True
                    else:
                        _fail(label, f"snapshot wrong: {snap!r}")
        except Exception as e:
            _fail(label, f"unexpected: {e}")
        if ok:
            _ok(label)
        results.append(ok)

        # --- Authenticated lifecycle control transport ---
        label = "authenticated node transport preserves exact lifecycle nonce"
        ok = False
        transport_error = "nonce was changed, omitted, or empty nonce was accepted"
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": "Bearer good-token"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 1, "node_id": "n1",
                }))
                assert json.loads(await ws.recv()).get("type") == "handshake"
                import node_link
                received_controls = []

                async def capture_control(**frame):
                    received_controls.append(frame)

                async def ignore_event(**_frame):
                    return None

                node_link.set_dispatchers(
                    run_control=capture_control, event_forward=ignore_event,
                )

                async def recv_type(expected):
                    deadline = asyncio.get_running_loop().time() + 3
                    while asyncio.get_running_loop().time() < deadline:
                        candidate = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                        if candidate.get("type") == expected:
                            return candidate
                    raise AssertionError(f"missing {expected} frame")

                await node_link.send_spawn_run("n1", {
                    "run_id": "wire-run", "lifecycle_nonce": "wire-nonce-1",
                })
                spawn_frame = await recv_type("spawn_run")
                await node_link.send_cancel_run(
                    "n1", "wire-run", lifecycle_nonce="wire-nonce-1",
                )
                frame = await recv_type("cancel_run")
                for control_type, data in (
                    ("accepted", {"lifecycle_nonce": "wire-nonce-1"}),
                    ("session_discovered", {
                        "lifecycle_nonce": "wire-nonce-1", "session_id": "wire-sid",
                    }),
                    ("error", {
                        "lifecycle_nonce": "wire-nonce-1", "error": "wire terminal",
                    }),
                ):
                    await ws.send(json.dumps({
                        "type": "run_control", "run_id": "wire-run",
                        "control_type": control_type, "data": data,
                    }))
                deadline = asyncio.get_running_loop().time() + 3
                while len(received_controls) < 3 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                missing_rejected = False
                try:
                    await node_link.send_cancel_run("n1", "wire-run", lifecycle_nonce="")
                except ValueError as exc:
                    missing_rejected = str(exc) == "lifecycle_nonce must be a non-empty string"
                ok = (
                    spawn_frame == {
                        "type": "spawn_run", "run_id": "wire-run",
                        "lifecycle_nonce": "wire-nonce-1",
                    }
                    and
                    frame == {
                        "type": "cancel_run", "run_id": "wire-run",
                        "lifecycle_nonce": "wire-nonce-1",
                    }
                    and missing_rejected
                    and [item["control_type"] for item in received_controls]
                    == ["accepted", "session_discovered", "error"]
                    and all(
                        item["data"]["lifecycle_nonce"] == "wire-nonce-1"
                        for item in received_controls
                    )
                )
                import provider_remote
                node_link.set_dispatchers(
                    run_control=provider_remote._on_run_control,
                    event_forward=provider_remote._on_event_forward,
                )
        except Exception as exc:
            transport_error = f"unexpected: {exc}"
            try:
                import node_link
                import provider_remote
                node_link.set_dispatchers(
                    run_control=provider_remote._on_run_control,
                    event_forward=provider_remote._on_event_forward,
                )
            except Exception:
                pass
        if ok:
            _ok(label)
        else:
            _fail(label, transport_error)
        results.append(ok)

        # ---------- POST /api/sessions node_id wiring ----------
        async def _post_session(body: dict) -> tuple[int, dict]:
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=10,
                headers=authed_headers,
            ) as c:
                r = await c.post("/api/sessions", json=body)
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, {"text": r.text}

        # Default fallback — no node_id in body → persisted as "primary".
        label = "POST /api/sessions without node_id defaults to primary"
        code, body = await _post_session({"cwd": "/tmp", "orchestration_mode": "manager"})
        ok = code == 200 and body.get("node_id") == "primary"
        if ok:
            _ok(label)
        else:
            _fail(label, f"code={code} body={body!r}")
        results.append(ok)

        # Happy path — node_id in topology, cwd matches cwd_roots.
        label = "POST /api/sessions with valid node_id persists it"
        code, body = await _post_session({
            "cwd": "/tmp", "orchestration_mode": "manager", "node_id": "n1",
        })
        ok = code == 200 and body.get("node_id") == "n1"
        if ok:
            _ok(label)
        else:
            _fail(label, f"code={code} body={body!r}")
        results.append(ok)

        # Unknown node_id → 400.
        label = "POST /api/sessions with unknown node_id rejects with 400"
        code, body = await _post_session({
            "cwd": "/tmp", "orchestration_mode": "manager", "node_id": "ghost",
        })
        ok = code == 400 and "ghost" in json.dumps(body)
        if ok:
            _ok(label)
        else:
            _fail(label, f"code={code} body={body!r}")
        results.append(ok)

        # cwd outside node's cwd_roots → 400.
        label = "POST /api/sessions cwd outside node cwd_roots rejects with 400"
        code, body = await _post_session({
            "cwd": "/etc", "orchestration_mode": "manager", "node_id": "n1",
        })
        ok = code == 400 and "cwd_roots" in json.dumps(body)
        if ok:
            _ok(label)
        else:
            _fail(label, f"code={code} body={body!r}")
        results.append(ok)

        # file_editing on a remote node now SUCCEEDS — file_editor.start
        # routes baseline reads through call_local_or_remote. Mock
        # rpc_call so we don't need a real n1 backend.
        label = "POST /api/sessions file_editing on remote node succeeds (via rpc_call)"
        import node_link
        _real_rpc_call = node_link.rpc_call
        async def _mock_rpc_call(node_id, method, params, *, timeout=30.0):
            if method == "file_editor_baseline":
                return {
                    "file_path_resolved": params["file_path"],
                    "cwd_resolved": params["cwd"] or "/tmp",
                    "original_content": "mock-baseline-content",
                }
            return await _real_rpc_call(node_id, method, params, timeout=timeout)
        node_link.rpc_call = _mock_rpc_call  # type: ignore[assignment]
        try:
            code, body = await _post_session({
                "cwd": "/tmp",
                "orchestration_mode": "native",
                "node_id": "n1",
                "file_edit_path": "/tmp/mock-fe.txt",
            })
            text = json.dumps(body)
            ok = (
                code == 200
                and body.get("node_id") == "n1"
                # Regression: old 400 detail string must be gone.
                and "primary node only" not in text
            )
            if ok:
                _ok(label)
            else:
                _fail(label, f"code={code} body={body!r}")
        finally:
            node_link.rpc_call = _real_rpc_call  # type: ignore[assignment]
        results.append(ok)

        # ---------- GET /api/local_node_id ----------
        label = "GET /api/local_node_id returns {node_id: 'primary'}"
        async with httpx.AsyncClient(base_url=base_url, timeout=5, headers=authed_headers) as c:
            r = await c.get("/api/local_node_id")
            data = r.json()
        ok = r.status_code == 200 and data.get("node_id") == "primary"
        if ok:
            _ok(label)
        else:
            _fail(label, f"code={r.status_code} body={data!r}")
        results.append(ok)

        # ---------- FS endpoint routing: local fast-path bypasses rpc_call ----------
        label = "FS endpoints with node_id=primary do NOT call rpc_call"
        rpc_call_invocations: list[tuple[str, str]] = []
        async def _spy_rpc_call(node_id, method, params, *, timeout=30.0):
            rpc_call_invocations.append((node_id, method))
            return await _real_rpc_call(node_id, method, params, timeout=timeout)
        node_link.rpc_call = _spy_rpc_call  # type: ignore[assignment]
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=5, headers=authed_headers) as c:
                # Default node_id (primary) — every endpoint stays local.
                await c.get("/api/browse", params={"path": "/tmp"})
                await c.get("/api/files", params={"path": "/tmp"})
                await c.get("/api/files/search", params={"root": "/tmp", "q": "z"})
                await c.get("/api/git-status", params={"cwd": "/tmp"})
                await c.get("/api/git-diff", params={"path": "/tmp", "cwd": "/tmp"})
                await c.get("/api/project-config", params={"cwd": "/tmp"})
            ok = len(rpc_call_invocations) == 0
            if ok:
                _ok(label)
            else:
                _fail(label, f"rpc_call invoked unexpectedly: {rpc_call_invocations}")
        finally:
            node_link.rpc_call = _real_rpc_call  # type: ignore[assignment]
        results.append(ok)

        # ---------- FS endpoint routing: remote node forwards to rpc_call ----------
        label = "FS endpoints with node_id=n1 route to rpc_call (all 9 endpoints, both query+body)"
        rpc_call_invocations = []
        async def _spy_route(node_id, method, params, *, timeout=30.0):
            rpc_call_invocations.append((node_id, method))
            # Return a valid-shaped result for whichever method.
            shapes = {
                "list_directories": {"path": "/tmp", "parent": None, "entries": []},
                "get_file_tree": {"name": "tmp", "path": "/tmp", "type": "directory", "children": []},
                "search_tree": {"root": None, "truncated": False, "count": 0, "symbols_unavailable": False},
                "get_file_content": {"content": "fake", "language": "plaintext", "path": "/tmp/x"},
                "write_file_content": {"path": "/tmp/x", "bytes": 4},
                "reconstruct_before_edit": {"before_content": "", "after_content": "", "language": "plaintext"},
                "get_git_status": {"is_git": False},
                "get_file_diff": {"diff": None},
                "scan_project_configs": [],
            }
            return shapes.get(method, {})
        node_link.rpc_call = _spy_route  # type: ignore[assignment]
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=5, headers=authed_headers) as c:
                # Query-shaped GETs:
                await c.get("/api/browse", params={"path": "/tmp", "node_id": "n1"})
                await c.get("/api/files", params={"path": "/tmp", "node_id": "n1"})
                await c.get("/api/files/search", params={"root": "/tmp", "q": "x", "node_id": "n1"})
                await c.get("/api/git-status", params={"cwd": "/tmp", "node_id": "n1"})
                await c.get("/api/git-diff", params={"path": "/tmp/x", "cwd": "/tmp", "node_id": "n1"})
                await c.get("/api/project-config", params={"cwd": "/tmp", "node_id": "n1"})
                await c.get("/api/file", params={"path": "/tmp/x", "node_id": "n1"})
                # Body-shaped POSTs:
                await c.post("/api/file", json={"path": "/tmp/x", "content": "hi", "node_id": "n1"})
                await c.post(
                    "/api/file-before-edit",
                    json={"file_path": "/tmp/x", "old_string": "a", "new_string": "b", "node_id": "n1"},
                )
            invoked_methods = sorted({m for _, m in rpc_call_invocations})
            expected = sorted([
                "list_directories", "get_file_tree", "search_tree",
                "get_git_status", "get_file_diff", "scan_project_configs",
                "get_file_content", "write_file_content", "reconstruct_before_edit",
            ])
            ok = invoked_methods == expected and all(n == "n1" for n, _ in rpc_call_invocations)
            if ok:
                _ok(label)
            else:
                _fail(
                    label,
                    f"got {invoked_methods}, expected {expected}, "
                    f"node_ids={set(n for n,_ in rpc_call_invocations)}",
                )
        finally:
            node_link.rpc_call = _real_rpc_call  # type: ignore[assignment]
        results.append(ok)

        # ---------- rpc_call exceptions → HTTP status mapping ----------
        label = "rpc_call exceptions map to correct HTTP status codes"
        async def _failing_rpc(exc):
            async def _impl(node_id, method, params, *, timeout=30.0):
                raise exc
            return _impl

        async def _hit_with_failing(exc):
            node_link.rpc_call = await _failing_rpc(exc)  # type: ignore[assignment]
            try:
                async with httpx.AsyncClient(base_url=base_url, timeout=5, headers=authed_headers) as c:
                    r = await c.get("/api/file", params={"path": "/tmp/x", "node_id": "n1"})
                    return r.status_code
            finally:
                node_link.rpc_call = _real_rpc_call  # type: ignore[assignment]

        offline_status = await _hit_with_failing(node_link.NodeOffline("offline"))
        timeout_status = await _hit_with_failing(asyncio.TimeoutError())
        runtime_status = await _hit_with_failing(RuntimeError("FileNotFoundError: missing"))
        ok = (
            offline_status == 503
            and timeout_status == 504
            and runtime_status == 502
        )
        if ok:
            _ok(label)
        else:
            _fail(
                label,
                f"offline={offline_status} (want 503), "
                f"timeout={timeout_status} (want 504), "
                f"runtime={runtime_status} (want 502)",
            )
        results.append(ok)

        # ---------- node_state_changed WS broadcast ----------
        # Uses node "n2" — DIFFERENT from the n1 the prior handshake
        # tests touched — so the listener's transition-only fire
        # semantic ("only fires when prev != new") doesn't depend on
        # prior-test cleanup ordering. Subscribe requires an existing
        # session id (the coordinator's `ws_callbacks` dict is keyed by
        # sid, and `broadcast_global` iterates over it — without at
        # least one subscription this client receives nothing).
        # Locks the WS contract the frontend useMachines hook depends
        # on, including the `last_seen` payload field.
        label = "node_state_changed broadcasts to /ws/chat subscribers on register"
        ok_connect = False
        ok_disconnect = False
        try:
            _, fresh = await _post_session({
                "cwd": "/tmp", "orchestration_mode": "manager",
            })
            sub_sid = fresh.get("id")
            assert sub_sid, f"could not create session for subscribe: {fresh!r}"
            async with websockets.connect(ws_url_chat) as chat_ws:
                await chat_ws.send(json.dumps({
                    "type": "subscribe",
                    "subscription_class": "foreground",
                    "app_session_id": sub_sid,
                    "cwd": "/tmp",
                }))
                # Drain the subscribe-triggered replay frames so the
                # main loop only sees frames that arrive AFTER we start
                # waiting. The replays are deterministic
                # (messages_replay + run_state, possibly
                # session_processing_*) — bounded by a 0.5s settle.
                try:
                    while True:
                        await asyncio.wait_for(chat_ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

                async def _trigger_handshake_cycle():
                    await asyncio.sleep(0.1)
                    async with websockets.connect(
                        ws_url,
                        additional_headers={"Authorization": "Bearer good-token"},
                    ) as node_ws:
                        await node_ws.send(json.dumps({
                            "type": "handshake",
                            "protocol_version": 1,
                            "node_id": "n2",
                        }))
                        await node_ws.recv()
                        # Hold briefly so the connected broadcast lands
                        # before we let the WS close (which fires the
                        # disconnect broadcast).
                        await asyncio.sleep(0.5)
                trigger = asyncio.create_task(_trigger_handshake_cycle())
                deadline = asyncio.get_event_loop().time() + 5.0
                while asyncio.get_event_loop().time() < deadline:
                    if ok_connect and ok_disconnect:
                        break
                    try:
                        raw = await asyncio.wait_for(chat_ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    frame = json.loads(raw)
                    if frame.get("type") != "node_state_changed":
                        continue
                    data = frame.get("data") or {}
                    if data.get("node_id") != "n2":
                        continue
                    # Connected payload MUST carry a real last_seen
                    # (backend reads conn.last_seen and includes it so
                    # the frontend never has to fabricate a timestamp).
                    if (
                        data.get("state") == "connected"
                        and isinstance(data.get("last_seen"), (int, float))
                    ):
                        ok_connect = True
                    # Disconnected payload carries last_seen=None
                    # (conn is gone by the time the listener fires);
                    # the frontend's `??` fallback preserves the prior
                    # heartbeat so the UI can still render "last seen
                    # Ns ago". Lock that contract.
                    if (
                        data.get("state") == "disconnected"
                        and data.get("last_seen") is None
                    ):
                        ok_disconnect = True
                await trigger
        except Exception as e:
            _fail(label, f"unexpected: {e}")
        ok = ok_connect and ok_disconnect
        if ok:
            _ok(label)
        else:
            _fail(
                label,
                f"connect_ok={ok_connect} disconnect_ok={ok_disconnect}",
            )
        results.append(ok)
    finally:
        server.stop()
        # Don't rm the home dir while the logger may still hold an open
        # FD on backend.log. The OS cleans /tmp later anyway.
    return results


# ==========================================================================
# Runner
# ==========================================================================

async def main() -> int:
    _section("Topology")
    sync_results = [
        test_topology_schema_mismatch_raises(),
        test_topology_missing_env_raises(),
        test_topology_local_node_id_unknown_raises(),
        test_resolve_known_spec_per_node_auth(),
    ]

    _section("Schema migrations")
    sync_results.extend([
        test_session_store_v7_default_node_id(),
        test_worker_store_v5_default_node_id(),
        test_pending_approvals_node_id_field(),
        test_provider_for_session_local_unchanged(),
        test_project_store_v2_round_trip(),
        test_project_store_v1_migrates_and_backs_up(),
        test_project_store_repairs_partial_v2_from_v1_backup(),
        test_project_store_repairs_despite_stale_marker(),
    ])

    _section("dispatch_rpc + filesystem routing")
    async_results = [
        await test_dispatch_rpc_json_serializability(),
        await test_dispatch_rpc_rejects_path_outside_cwd_roots(),
        await test_file_op_no_topology_routes_remote_not_local(),
        await test_file_op_no_topology_routes_connected_dynamic_node(),
        await test_run_headless_rewind_rpc_wiring(),
        await test_provisioning_node_id_routing(),
    ]

    _section("shadow_jsonl")
    async_results.extend([
        await test_shadow_append_simple(),
        await test_shadow_partial_line_recovery(),
        await test_shadow_version_bump_truncates(),
        await test_shadow_concurrent_writes_serialize(),
    ])

    _section("Handshake")
    async_results.extend(await run_handshake_tests())

    all_results = sync_results + async_results
    failed = sum(1 for r in all_results if not r)
    total = len(all_results)
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

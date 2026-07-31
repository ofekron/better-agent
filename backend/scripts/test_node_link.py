"""Unit coverage for node_link.py — the primary-side node↔primary link.

Covers the pure auth/security helpers (loopback + credential-sync gating,
incident rejection rules, public-record projection/expiry, deny-cooldown),
the registration-approval lifecycle (resolve/approve/deny/await against
seeded stores), inbound message routing + offset accounting, the outbound
send/rpc API (incl. NodeOffline and version/secure-transport guards), and
the REST runtime-operations authority gate.

Isolation: real BETTER_AGENT_HOME tempdir, real pending_node_registrations
+ node_registry_store persistence, in-memory node_store/shadow_jsonl reset
per test. No real WebSocket / backend / network.
"""
import asyncio
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="node_link_")
import atexit  # noqa: E402

atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
paths.engage_test_home(_TMP)

import node_link  # noqa: E402
import node_store  # noqa: E402
from stores import pending_node_registrations  # noqa: E402
import node_registry_store  # noqa: E402
import shadow_jsonl  # noqa: E402
from topology import NodeSpec  # noqa: E402


def _reset_modules():
    """Clear every module-level registry between tests."""
    node_store.reset_for_tests()
    shadow_jsonl.reset_for_tests()
    node_link._node_approval_waiters.clear()
    node_link._public_pending_cache = None
    node_link._machine_nodes_ready_cache = (0.0, None)
    node_link._registration_listener = None
    node_link.set_dispatchers(run_control=None, event_forward=None)


# ---------------------------------------------------------------- helpers


def _spec(node_id="node-a", role="worker_node", roots=("/repo",)):
    return NodeSpec(id=node_id, role=role, address="1.2.3.4", cwd_roots=roots)


class _FakeURL:
    def __init__(self, scheme="ws"):
        self.scheme = scheme


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeWS:
    """Minimal WebSocket stand-in for send/client/url + receive_json."""

    def __init__(self, *, scheme="ws", host="127.0.0.1", inbox=None):
        self.url = _FakeURL(scheme)
        self.client = _FakeClient(host)
        self.sent = []
        self._inbox = inbox or []
        self.closed = False

    async def send_json(self, obj):
        self.sent.append(obj)

    async def receive_json(self):
        if not self._inbox:
            await asyncio.sleep(0.05)
            raise asyncio.TimeoutError
        return self._inbox.pop(0)

    async def close(self, **kwargs):
        self.closed = True


def _conn(node_id="node-a", *, ws=None, runtime_ready=False, roots=("/repo",)):
    ws = ws or _FakeWS()
    spec = _spec(node_id, roots=roots)
    conn = node_store.NodeConnection(
        spec=spec, ws=ws, connected_at=time.monotonic(), last_seen=time.monotonic()
    )
    conn.runtime_ready = runtime_ready
    node_store._conns[node_id] = conn
    return conn


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _seed_pending(node_id="node-pending", *, secret="s3cret", cwd_roots=("/repo",)):
    secret_hash = node_registry_store.hash_secret(secret)
    return pending_node_registrations.create(
        node_id=node_id,
        address="10.0.0.5",
        cwd_roots=list(cwd_roots),
        secret_hash=secret_hash,
        fingerprint="abcdef123456",
    )


def _seed_registry(node_id="node-known", *, secret="s3cret", cwd_roots=("/repo",)):
    return node_registry_store.add(
        node_id=node_id,
        address="10.0.0.6",
        cwd_roots=list(cwd_roots),
        secret_hash=node_registry_store.hash_secret(secret),
    )


# ============================================================ _public_rec


def test_public_rec_strips_secret_and_projects_fields():
    rec = {
        "node_id": "n1",
        "address": "1.1.1.1",
        "cwd_roots": ["/r"],
        "fingerprint": "fp",
        "status": "pending",
        "created_at": "t1",
        "expires_at": "t2",
        "secret_hash": "MUST-NOT-LEAK",
    }
    out = node_link._public_rec(rec)
    assert "secret_hash" not in out
    assert out["node_id"] == "n1"
    assert out["cwd_roots"] == ["/r"]
    missing = {"node_id", "address", "cwd_roots", "fingerprint", "status",
               "created_at", "expires_at"}
    assert missing <= set(out)


# ======================================================== _public_record_expired


def test_public_record_expired_branches():
    now = datetime.now()
    assert node_link._public_record_expired({"expires_at": (now - timedelta(seconds=1)).isoformat()}) is True
    assert node_link._public_record_expired({"expires_at": (now + timedelta(days=1)).isoformat()}) is False
    assert node_link._public_record_expired({}) is False  # no expires_at
    assert node_link._public_record_expired({"expires_at": "not-a-date"}) is False
    assert node_link._public_record_expired({"expires_at": None}) is False


# ============================================================ public_pending


def test_public_pending_nodes_projects_and_caches():
    try:
        _seed_pending("pend-a")
        _seed_pending("pend-b")
        listed = node_link.public_pending_nodes()
        ids = {r["node_id"] for r in listed}
        assert {"pend-a", "pend-b"} <= ids
        assert all("secret_hash" not in r for r in listed)
        # Cached read returns the same set without re-reading the store.
        assert node_link.public_pending_nodes_cached() is not None
        cached = node_link.public_pending_nodes_cached()
        assert {r["node_id"] for r in cached} == ids
    finally:
        node_link._public_pending_cache = None


def test_public_pending_cache_invalidates_on_version_change():
    try:
        _seed_pending("pend-x")
        first = node_link.public_pending_nodes()
        assert {"pend-x"} <= {r["node_id"] for r in first}
        # New pending bumps the store version -> cached read must miss.
        assert node_link.public_pending_nodes_cached() is not None
        _seed_pending("pend-y")
        assert node_link.public_pending_nodes_cached() is None  # version changed
        second = node_link.public_pending_nodes()
        assert {"pend-x", "pend-y"} <= {r["node_id"] for r in second}
    finally:
        node_link._public_pending_cache = None


# ============================================================ _recently_denied


def test_recently_denied_branches():
    now = datetime.now()
    assert node_link._recently_denied(None) is False
    assert node_link._recently_denied({}) is False
    assert node_link._recently_denied({"status": "pending"}) is False
    # Denied without resolved_at -> still cooling down.
    assert node_link._recently_denied({"status": "denied"}) is True
    # Denied recently.
    recent = {"status": "denied", "resolved_at": now.isoformat()}
    assert node_link._recently_denied(recent) is True
    # Denied long ago -> cooled down.
    old = {"status": "denied",
           "resolved_at": (now - timedelta(seconds=node_link._DENY_COOLDOWN_S + 1)).isoformat()}
    assert node_link._recently_denied(old) is False
    # Denied with malformed resolved_at -> treated as still cooling.
    assert node_link._recently_denied({"status": "denied", "resolved_at": "garbage"}) is True


# ============================================================ _is_loopback_host


def test_is_loopback_host_branches():
    assert node_link._is_loopback_host("localhost") is True
    assert node_link._is_loopback_host("127.0.0.1") is True
    assert node_link._is_loopback_host("::1") is True
    assert node_link._is_loopback_host("10.0.0.5") is False
    assert node_link._is_loopback_host("not-an-ip") is False
    assert node_link._is_loopback_host("") is False
    assert node_link._is_loopback_host(None) is False
    assert node_link._is_loopback_host(12345) is False


# ================================================ connection_allows_credential_sync


def test_connection_allows_credential_sync_branches():
    # WSS always allowed.
    conn = _conn("n-wss", ws=_FakeWS(scheme="wss", host="10.0.0.9"))
    assert node_link.connection_allows_credential_sync(conn) is True
    # Plain ws + loopback client host -> allowed.
    conn = _conn("n-loop", ws=_FakeWS(scheme="ws", host="127.0.0.1"))
    assert node_link.connection_allows_credential_sync(conn) is True
    # Plain ws + non-loopback -> denied.
    conn = _conn("n-remote", ws=_FakeWS(scheme="ws", host="203.0.113.7"))
    assert node_link.connection_allows_credential_sync(conn) is False
    node_store.reset_for_tests()

    # ws.client as a list/tuple (no .host attr) -> host from first element.
    class _ListClientWS:
        url = _FakeURL("ws")
        client = ["127.0.0.1", 9000]

    conn = node_store.NodeConnection(
        spec=_spec("n-list"), ws=_ListClientWS(),
        connected_at=0.0, last_seen=0.0,
    )
    assert node_link.connection_allows_credential_sync(conn) is True

    class _ListClientWSRemote:
        url = _FakeURL("ws")
        client = ["8.8.8.8", 9000]

    conn = node_store.NodeConnection(
        spec=_spec("n-list2"), ws=_ListClientWSRemote(),
        connected_at=0.0, last_seen=0.0,
    )
    assert node_link.connection_allows_credential_sync(conn) is False


# ============================================================ _primary_id


def test_primary_id_falls_back_without_topology():
    # No BETTER_CLAUDE_TOPOLOGY_PATH -> load_topology raises -> "primary".
    assert node_link._primary_id() == "primary"


# ============================================================ _machine_nodes_not_ready


def test_machine_nodes_not_ready_none_when_no_machine_nodes_role():
    try:
        # Fresh home has no machine-nodes extension role -> None.
        assert node_link._machine_nodes_not_ready_reason() is None
    finally:
        node_link._machine_nodes_ready_cache = (0.0, None)


def test_machine_nodes_not_ready_caches_reason():
    import types
    fake = types.ModuleType("extension_store")
    fake.extension_id_for_role = lambda role: "ext-machine" if role == "machine-nodes" else None
    fake.runtime_not_ready_message = lambda eid: "not ready"
    saved = sys.modules.get("extension_store")
    sys.modules["extension_store"] = fake
    try:
        node_link._machine_nodes_ready_cache = (0.0, None)
        first = node_link._machine_nodes_not_ready_reason()
        assert first == "not ready"
        # Second call within the TTL is served from cache.
        assert node_link._machine_nodes_not_ready_reason() == "not ready"
    finally:
        if saved is not None:
            sys.modules["extension_store"] = saved
        else:
            sys.modules.pop("extension_store", None)
        node_link._machine_nodes_ready_cache = (0.0, None)


# ============================================================ _emit_registration


def test_emit_registration_noop_without_listener():

    async def _scenario():
        await node_link._emit_registration("x", {"a": 1})  # no listener -> no raise

    _run(_scenario())


def test_emit_registration_invokes_listener_and_swallows_error():
    calls = []

    async def _listener(event_type, payload):
        calls.append((event_type, payload))
        if event_type == "boom":
            raise RuntimeError("listener broke")

    node_link.set_registration_listener(_listener)

    async def _scenario():
        await node_link._emit_registration("ok", {"a": 1})
        await node_link._emit_registration("boom", {"a": 2})  # swallowed

    try:
        _run(_scenario())
        assert ("ok", {"a": 1}) in calls
        assert ("boom", {"a": 2}) in calls
    finally:
        node_link._registration_listener = None


# ============================================================ _authoritative_registered_spec


def _fake_topo(nodes):
    class _T:
        primary = type("P", (), {"id": "primary"})()

        def all_nodes(self):
            return nodes

    return _T()


def test_authoritative_registered_spec_topology_worker_and_nonworker():
    nodes = {
        "w": NodeSpec("w", "worker_node", "1.1.1.1", ("/r",)),
        "p": NodeSpec("p", "primary", "2.2.2.2", ()),
    }
    topo = _fake_topo(nodes)
    spec, err = node_link._authoritative_registered_spec("w", topo)
    assert err is None and spec.id == "w"
    spec, err = node_link._authoritative_registered_spec("p", topo)
    assert spec is None and "not worker_node" in err


def test_authoritative_registered_spec_registry_and_vanished():
    # No topology -> falls back to registry record.
    _seed_registry("reg-1", cwd_roots=("/repo-reg",))
    spec, err = node_link._authoritative_registered_spec("reg-1", None)
    assert err is None and spec.id == "reg-1"
    assert spec.cwd_roots == ("/repo-reg",)
    # Registry record vanished.
    spec, err = node_link._authoritative_registered_spec("ghost", None)
    assert spec is None and err == "registry record vanished"


# ============================================================ _resolve_known_spec


def test_resolve_known_spec_topology_nonworker_rejected():
    topo = _fake_topo({"p": NodeSpec("p", "primary", "1.1.1.1", ())})
    orig = node_link.load_topology
    node_link.load_topology = lambda: topo
    try:
        spec, err = node_link._resolve_known_spec("p", "any")
        assert spec is None and "not worker_node" in err
    finally:
        node_link.load_topology = orig


def test_resolve_known_spec_topology_undeclared_rejected():
    topo = _fake_topo({"w": NodeSpec("w", "worker_node", "1.1.1.1", ())})
    orig = node_link.load_topology
    node_link.load_topology = lambda: topo
    try:
        spec, err = node_link._resolve_known_spec("intruder", "any")
        assert spec is None and "not declared in topology.yaml" in err
    finally:
        node_link.load_topology = orig


def test_resolve_known_spec_topology_declared_not_yet_approved_pends():
    # Declared in topology but not yet in registry -> (None, None) = run approval flow.
    topo = _fake_topo({"w": NodeSpec("w", "worker_node", "1.1.1.1", ("/t",))})
    orig = node_link.load_topology
    node_link.load_topology = lambda: topo
    try:
        spec, err = node_link._resolve_known_spec("w", "ignored-until-approved")
        assert spec is None and err is None
    finally:
        node_link.load_topology = orig


def test_resolve_known_spec_registry_bad_secret_rejected():
    _seed_registry("kn", secret="right")
    spec, err = node_link._resolve_known_spec("kn", "wrong")
    assert spec is None and err == "bad secret"


def test_resolve_known_spec_registry_unknown_dynamic_pends():
    # No topology; not in registry -> (None, None) = approval flow.
    assert node_link._resolve_known_spec("brand-new", "s") == (None, None)


def test_resolve_known_spec_registry_good_secret_resolves():
    _seed_registry("kn2", secret="right", cwd_roots=("/repo-x",))
    spec, err = node_link._resolve_known_spec("kn2", "right")
    assert err is None and spec is not None and spec.id == "kn2"
    assert spec.cwd_roots == ("/repo-x",)


# ============================================================ approve / deny


def test_approve_registration_persists_and_resolves_waiter():
    _seed_pending("pend-app")
    # A waiter is holding for this node.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    fut = loop.create_future()
    node_link._node_approval_waiters["pend-app"] = fut
    try:
        rec, reason = loop.run_until_complete(node_link.approve_registration("pend-app"))
        assert reason == "ok"
        assert rec["status"] == "approved"
        assert fut.result().id == "pend-app"
        # Now registered -> future reconnects auto-auth.
        assert node_registry_store.get("pend-app") is not None
    finally:
        node_link._node_approval_waiters.pop("pend-app", None)
        loop.close()


def test_approve_registration_idempotent_second_call_no_op():
    _seed_pending("pend-id")
    _run(node_link.approve_registration("pend-id"))
    rec, reason = _run(node_link.approve_registration("pend-id"))
    assert reason != "ok"  # already approved -> store returns non-ok


def test_deny_registration_resolves_waiter_none_and_emits():
    _seed_pending("pend-deny")
    emitted = []

    async def _listener(e, p):
        emitted.append((e, p))

    node_link.set_registration_listener(_listener)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    fut = loop.create_future()
    node_link._node_approval_waiters["pend-deny"] = fut
    try:
        rec, reason = loop.run_until_complete(node_link.deny_registration("pend-deny"))
        assert reason == "ok"
        assert rec["status"] == "denied"
        assert fut.result() is None  # denied -> None spec
        assert any(e == "node_registration_resolved" for e, _ in emitted)
    finally:
        node_link._node_approval_waiters.pop("pend-deny", None)
        node_link._registration_listener = None
        loop.close()


def test_deny_registration_no_op_when_not_pending():
    rec, reason = _run(node_link.deny_registration("never-seen"))
    assert reason != "ok"
    assert rec is None


def test_approve_registration_rejects_non_worker_topology_role():
    topo = _fake_topo({"p": NodeSpec("p", "primary", "1.1.1.1", ())})
    _seed_pending("p")  # pending record exists so we reach the role guard
    orig = node_link.load_topology
    node_link.load_topology = lambda: topo
    try:
        try:
            _run(node_link.approve_registration("p"))
            assert False, "expected ValueError for non-worker role"
        except ValueError as exc:
            assert "not worker_node" in str(exc)
    finally:
        node_link.load_topology = orig


# ============================================================ _resolve_waiter


def test_resolve_waiter_no_waiter_returns_false():
    assert node_link._resolve_waiter("none", _spec("none")) is False


# ============================================================ _await_registration


def test_await_registration_rejects_missing_secret():

    async def _scenario():
        return await node_link._await_registration(_FakeWS(), "n", "", {})

    assert _run(_scenario()) is None


def test_await_registration_rejects_recently_denied():
    # Seed a denied record still in cooldown.
    pending_node_registrations.create(
        node_id="denied-n", address="", cwd_roots=["/r"],
        secret_hash=node_registry_store.hash_secret("s"),
        fingerprint="abcdef123456",
    )
    _, _ = pending_node_registrations.deny("denied-n")

    async def _scenario():
        return await node_link._await_registration(_FakeWS(), "denied-n", "s", {})

    try:
        assert _run(_scenario()) is None
    finally:
        _reset_modules()


def test_await_registration_rejects_bad_cwd_roots():

    async def _scenario():
        # cwd_roots with a relative path triggers NodePathError -> None.
        return await node_link._await_registration(
            _FakeWS(), "bad-roots", "s", {"cwd_roots": ["relative/path"]}
        )

    assert _run(_scenario()) is None


def test_await_registration_happy_path_resolves_on_approval():
    ws = _FakeWS()

    async def _scenario():
        # Run await in a task; approve it mid-flight from the outside.
        task = asyncio.ensure_future(
            node_link._await_registration(ws, "happy-n", "topsecret",
                                          {"address": "9.9.9.9", "cwd_roots": [str(Path(_TMP))]})
        )
        await asyncio.sleep(0.02)  # let it register a waiter + send pending frame
        # Approve via the public path so the waiter is resolved with a spec.
        await node_link.approve_registration("happy-n")
        return await asyncio.wait_for(task, timeout=2.0)

    try:
        spec = _run(_scenario())
        assert spec is not None and spec.id == "happy-n"
        # The node was told it is pending.
        assert any(f.get("type") == "registration_pending" for f in ws.sent)
    finally:
        _reset_modules()


def test_await_registration_returns_none_when_send_fails():

    class _BrokenWS(_FakeWS):
        async def send_json(self, obj):
            raise OSError("ws gone")

    async def _scenario():
        return await node_link._await_registration(
            _BrokenWS(), "sendfail-n", "s",
            {"cwd_roots": [str(Path(_TMP))]},
        )

    try:
        assert _run(_scenario()) is None
        # The pending record was still created before the send attempt.
        assert pending_node_registrations.get("sendfail-n") is not None
    finally:
        _reset_modules()


def test_await_registration_times_out_without_approval():
    node_link.REGISTRATION_TIMEOUT_S = 0.05
    ws = _FakeWS()

    async def _scenario():
        return await node_link._await_registration(
            ws, "to-n", "s", {"cwd_roots": [str(Path(_TMP))]})

    try:
        assert _run(_scenario()) is None
        # The waiter was removed from the registry on the way out.
        assert "to-n" not in node_link._node_approval_waiters
    finally:
        node_link.REGISTRATION_TIMEOUT_S = 600.0
        _reset_modules()


# ============================================================ _incident_rejection_reason

def _valid_incident(now, *, kind="backend_timeout", **overrides):
    msg = {
        "incident_id": "a" * 32,
        "extension_id": "ext.one",
        "activation_id": "b" * 32,
        "kind": kind,
        "elapsed_seconds": 1.0,
        "occurred_at": now,
    }
    if kind == "slow_backend_call":
        msg["path"] = "/api/x"
    msg.update(overrides)
    return msg


def test_incident_rejection_valid_empty_reason():
    now = time.time()
    assert node_link._incident_rejection_reason(_valid_incident(now), now=now) == ""
    assert node_link._incident_rejection_reason(
        _valid_incident(now, kind="slow_backend_call"), now=now) == ""


def test_incident_rejection_capacity_for_oversize():
    now = time.time()
    msg = _valid_incident(now)
    msg["junk"] = "x" * (node_link._EXTENSION_INCIDENT_MAX_PAYLOAD_BYTES + 10)
    assert node_link._incident_rejection_reason(msg, now=now) == "capacity"


def test_incident_rejection_malformed_field_variants():
    now = time.time()
    bad_id = _valid_incident(now, incident_id="nothex")
    assert node_link._incident_rejection_reason(bad_id, now=now) == "malformed"
    bad_ext = _valid_incident(now, extension_id="UPPER")
    assert node_link._incident_rejection_reason(bad_ext, now=now) == "malformed"
    bad_kind = _valid_incident(now, kind="weird")
    assert node_link._incident_rejection_reason(bad_kind, now=now) == "malformed"
    bad_elapsed = _valid_incident(now, elapsed_seconds=float("nan"))
    assert node_link._incident_rejection_reason(bad_elapsed, now=now) == "malformed"
    neg_elapsed = _valid_incident(now, elapsed_seconds=-1.0)
    assert node_link._incident_rejection_reason(neg_elapsed, now=now) == "malformed"
    bool_elapsed = _valid_incident(now, elapsed_seconds=True)  # bool not allowed
    assert node_link._incident_rejection_reason(bool_elapsed, now=now) == "malformed"
    # slow_backend_call requires a non-empty path <= 512.
    no_path = _valid_incident(now, kind="slow_backend_call")
    no_path.pop("path")
    assert node_link._incident_rejection_reason(no_path, now=now) == "malformed"
    long_path = _valid_incident(now, kind="slow_backend_call", path="z" * 600)
    assert node_link._incident_rejection_reason(long_path, now=now) == "malformed"


def test_incident_rejection_future_skew_and_stale():
    now = time.time()
    future = _valid_incident(now + node_link._EXTENSION_INCIDENT_FUTURE_SKEW_SECONDS + 5)
    assert node_link._incident_rejection_reason(future, now=now) == "future_skew"
    stale = _valid_incident(now - node_link._EXTENSION_INCIDENT_MAX_AGE_SECONDS - 5)
    assert node_link._incident_rejection_reason(stale, now=now) == "stale"


# ============================================================ _handle_extension_incident


def test_handle_extension_incident_rejects_malformed_and_acks():
    conn = _conn("inc-n")
    msg = _valid_incident(time.time(), incident_id="nothex")

    async def _scenario():
        await node_link._handle_extension_incident("inc-n", msg)

    try:
        _run(_scenario())
        ack = conn.ws.sent[-1]
        assert ack["accepted"] is False
        assert ack["reason"] == "malformed"
    finally:
        _reset_modules()


def test_ack_extension_incident_no_connection_is_noop():
    # No registered connection -> returns without raising.
    async def _scenario():
        await node_link._ack_extension_incident("ghost", "id", accepted=True, reason="handled")

    _run(_scenario())  # no raise


def test_handle_extension_incident_records_and_acks_both_kinds():
    import types
    now = time.time()
    conn = _conn("inc-ok")
    disabled = {"v": False}
    fake_ext = types.ModuleType("extension_store")
    fake_ext.record_backend_timeout = lambda eid, **kw: disabled["v"]
    fake_ext.record_slow_backend_call = lambda eid, *, path, **kw: disabled["v"]
    fake_api = types.ModuleType("extension_api")
    fake_api.EXTENSION_CATALOG_TOPICS = ("topic1",)
    broadcast = []

    async def _bcast(*topics):
        broadcast.append(topics)

    fake_api._broadcast_extension_changed = _bcast
    fake_ncs = types.ModuleType("node_config_sync")
    ncs = []
    fake_ncs.notify_changed = lambda key: ncs.append(key)
    saved = {m: sys.modules.get(m) for m in ("extension_store", "extension_api", "node_config_sync")}
    sys.modules["extension_store"] = fake_ext
    sys.modules["extension_api"] = fake_api
    sys.modules["node_config_sync"] = fake_ncs

    async def _timeout():
        await node_link._handle_extension_incident("inc-ok", _valid_incident(now))

    async def _slow():
        await node_link._handle_extension_incident(
            "inc-ok", _valid_incident(now, kind="slow_backend_call", path="/api/y"))

    try:
        # backend_timeout, not disabling -> handled, no broadcast.
        _run(_timeout())
        assert conn.ws.sent[-1] == {"type": "extension_incident_ack",
                                    "incident_id": "a" * 32,
                                    "accepted": True, "reason": "handled"}
        assert broadcast == []

        # slow_backend_call that disables -> broadcast + notify.
        disabled["v"] = True
        _run(_slow())
        assert broadcast == [("topic1",)]
        assert ncs == ["extensions"]
        assert conn.ws.sent[-1]["accepted"] is True

        # record_* raising is swallowed (no ack crash); ack still NOT sent on exception.
        disabled["v"] = False

        def _boom(eid, **kw):
            raise RuntimeError("store down")
        fake_ext.record_backend_timeout = _boom

        async def _err():
            await node_link._handle_extension_incident("inc-ok", _valid_incident(now))
        _run(_err())  # no raise
    finally:
        for m, mod in saved.items():
            if mod is not None:
                sys.modules[m] = mod
            else:
                sys.modules.pop(m, None)
        _reset_modules()


# ============================================================ inbound routing


def test_route_inbound_unknown_type_warns():
    _conn("unk-n")

    async def _scenario():
        await node_link._route_inbound("unk-n", {"type": "mystery"})

    try:
        _run(_scenario())  # no raise; logged
    finally:
        _reset_modules()


def test_route_inbound_pong_is_noop():
    _conn("pong-n")

    async def _scenario():
        await node_link._route_inbound("pong-n", {"type": "pong"})

    try:
        _run(_scenario())
    finally:
        _reset_modules()


def test_route_inbound_ping_sends_pong():
    conn = _conn("ping-n")

    async def _scenario():
        await node_link._route_inbound("ping-n", {"type": "ping", "ts": 42})

    try:
        _run(_scenario())
        assert conn.ws.sent[-1]["type"] == "pong"
        assert conn.ws.sent[-1]["ts"] == 42
    finally:
        _reset_modules()


def test_route_inbound_dispatches_every_typed_handler():
    """Drive each msg_type through the public router so every dispatch
    branch is exercised end-to-end (not just the handlers directly)."""
    _conn("rt-all")
    import event_journal

    async def _noop_publish(**kw):
        return None

    event_journal.publish_event = _noop_publish
    seen = []

    async def _rc(**kw):
        seen.append(("rc", kw.get("run_id")))

    async def _ef(**kw):
        seen.append(("ef", kw.get("run_id")))

    node_link.set_dispatchers(run_control=_rc, event_forward=_ef)

    async def _scenario():
        await node_link._route_inbound("rt-all", {"type": "event_forward",
                                                   "root_id": "r", "run_id": "r1"})
        await node_link._route_inbound("rt-all", {
            "type": "jsonl_line", "root_id": "r", "fork_agent_sid": "f",
            "file_version": 1, "line_offset_in_version": 0, "line": "{}\n"})
        await node_link._route_inbound("rt-all", {"type": "run_control", "run_id": "r2"})
        # Malformed incident -> ack rejected (no crash).
        await node_link._route_inbound("rt-all", {"type": "extension_incident"})
        # rpc_response with no pending request -> noop.
        await node_link._route_inbound("rt-all", {"type": "rpc_response", "request_id": "x"})

    try:
        _run(_scenario())
        assert ("rc", "r2") in seen
        assert ("ef", "r1") in seen
    finally:
        node_link.set_dispatchers(run_control=None, event_forward=None)
        _reset_modules()


def test_handle_event_forward_swallows_journal_exception():
    _conn("ef-err-n")
    import event_journal

    async def _boom(**kw):
        raise RuntimeError("journal down")
    event_journal.publish_event = _boom

    async def _scenario():
        # Valid event whose journal write raises -> swallowed, offset NOT advanced.
        await node_link._handle_event_forward(
            "ef-err-n",
            {"type": "event_forward", "root_id": "root-err", "event_type": "x",
             "node_offset": 9})

    try:
        _run(_scenario())  # no raise
        conn = node_store.get_connection("ef-err-n")
        assert "root-err" not in conn.last_acked_offset  # return before accounting
    finally:
        _reset_modules()


def test_handle_jsonl_line_swallows_append_exception():
    _conn("jl-err-n")
    orig_append = shadow_jsonl.append

    async def _boom(**kw):
        raise RuntimeError("io down")
    shadow_jsonl.append = _boom  # type: ignore

    async def _scenario():
        await node_link._handle_jsonl_line(
            "jl-err-n",
            {"root_id": "root-err", "fork_agent_sid": "f", "file_version": 1,
             "line_offset_in_version": 0, "line": "{}\n", "node_offset": 4})

    try:
        _run(_scenario())  # no raise; offset not advanced on failure
        conn = node_store.get_connection("jl-err-n")
        assert conn.last_acked_offset.get("root-err", 0) == 0
    finally:
        shadow_jsonl.append = orig_append  # type: ignore
        _reset_modules()


def test_handlers_noop_when_connection_dropped_mid_message():
    """A node whose conn vanished between touch and a handler that reads
    conn state must not crash — every handler guards get_connection."""
    import event_journal

    async def _noop_publish(**kw):
        return None

    event_journal.publish_event = _noop_publish

    async def _scenario():
        # No conn registered for 'gone-n' anywhere in these calls.
        await node_link._route_inbound("gone-n", {"type": "ping", "ts": 1})
        await node_link._handle_event_forward(
            "gone-n", {"type": "event_forward", "root_id": "r", "node_offset": 5})
        await node_link._handle_jsonl_line(
            "gone-n", {"root_id": "r", "fork_agent_sid": "f", "file_version": 1,
                       "line_offset_in_version": 0, "line": "{}\n", "node_offset": 5})

    try:
        _run(_scenario())  # no raise across all three no-conn paths
    finally:
        _reset_modules()


def test_handle_event_forward_swallows_dispatcher_exception():
    _conn("efd-err-n")
    import event_journal

    async def _noop_publish(**kw):
        return None

    event_journal.publish_event = _noop_publish

    async def _fwd(**kw):
        raise RuntimeError("dispatcher broke")

    node_link.set_dispatchers(run_control=None, event_forward=_fwd)

    async def _scenario():
        await node_link._handle_event_forward(
            "efd-err-n", {"type": "event_forward", "root_id": "r",
                          "run_id": "r1", "event_type": "t"})

    try:
        _run(_scenario())  # dispatcher exception swallowed
    finally:
        node_link.set_dispatchers(run_control=None, event_forward=None)
        _reset_modules()


def test_handle_run_control_warns_when_dispatcher_unwired():
    _conn("rc-n")

    async def _scenario():
        await node_link._handle_run_control("rc-n", {"run_id": "r1", "control_type": "cancel"})

    try:
        _run(_scenario())  # no raise; logs warning (dispatcher None)
    finally:
        _reset_modules()


def test_handle_run_control_dispatches_and_swallows_error():
    _conn("rc2-n")
    calls = []

    async def _disp(**kw):
        calls.append(kw)
        if kw.get("control_type") == "boom":
            raise RuntimeError("disp broke")

    node_link.set_dispatchers(run_control=_disp, event_forward=None)

    async def _scenario():
        await node_link._handle_run_control("rc2-n", {"run_id": "r1", "control_type": "cancel"})
        await node_link._handle_run_control("rc2-n", {"run_id": "r2", "control_type": "boom"})

    try:
        _run(_scenario())
        assert calls[0]["run_id"] == "r1"
        assert len(calls) == 2  # error swallowed, not raised
    finally:
        _reset_modules()


def test_handle_event_forward_ignores_non_str_root_and_advances_offset():
    conn = _conn("ef-n")
    # Patch the lazy event_journal.publish_event import target.
    import event_journal

    published = []

    async def _fake_publish(**kw):
        published.append(kw)

    orig = event_journal.publish_event
    event_journal.publish_event = _fake_publish

    async def _scenario():
        # Non-str root_id -> ignored entirely.
        await node_link._handle_event_forward("ef-n", {"type": "event_forward", "root_id": 123})
        # Valid event with an advancing node_offset.
        await node_link._handle_event_forward(
            "ef-n",
            {"type": "event_forward", "root_id": "root-1", "sid": "s1",
             "event_type": "x", "data": {"a": 1}, "run_id": "r1", "node_offset": 5},
        )
        # Lower offset must not rewind the cursor.
        await node_link._handle_event_forward(
            "ef-n",
            {"type": "event_forward", "root_id": "root-1", "event_type": "x",
             "node_offset": 2},
        )

    try:
        _run(_scenario())
        # The non-str-root call did not publish; the two valid ones did.
        assert len(published) == 2
        assert published[0]["session_id"] == "root-1"
        # The lower-offset event did not rewind the cursor.
        assert conn.last_acked_offset["root-1"] == 5
    finally:
        event_journal.publish_event = orig
        _reset_modules()


def test_handle_event_forward_dispatcher_invoked_for_run():
    _conn("efd-n")
    import event_journal

    async def _noop_publish(**kw):
        return None

    event_journal.publish_event = _noop_publish
    fwd_calls = []

    async def _fwd(**kw):
        fwd_calls.append(kw)

    node_link.set_dispatchers(run_control=None, event_forward=_fwd)

    async def _scenario():
        await node_link._handle_event_forward(
            "efd-n", {"type": "event_forward", "root_id": "r", "run_id": "r1",
                      "event_type": "t", "data": {}},
        )
        # No run_id -> dispatcher NOT invoked.
        fwd_before = len(fwd_calls)
        await node_link._handle_event_forward(
            "efd-n", {"type": "event_forward", "root_id": "r", "event_type": "t"},
        )
        assert len(fwd_calls) == fwd_before

    try:
        _run(_scenario())
        assert fwd_calls and fwd_calls[0]["run_id"] == "r1"
    finally:
        node_link.set_dispatchers(run_control=None, event_forward=None)
        _reset_modules()


def test_handle_jsonl_line_missing_key_swallowed():
    _conn("jl-n")

    async def _scenario():
        # Missing fork_agent_sid -> KeyError swallowed.
        await node_link._handle_jsonl_line("jl-n", {"root_id": "r", "file_version": 1})

    try:
        _run(_scenario())  # no raise
    finally:
        _reset_modules()


def test_handle_jsonl_line_appends_and_advances_offset():
    conn = _conn("jl2-n")
    shadow_jsonl.reset_for_tests()

    async def _scenario():
        await node_link._handle_jsonl_line(
            "jl2-n",
            {"root_id": "root-j", "fork_agent_sid": "fork-1", "file_version": 1,
             "line_offset_in_version": 0, "line": "{}\n", "node_offset": 3},
        )

    try:
        _run(_scenario())
        assert conn.last_acked_offset["root-j"] == 3
        cursors = shadow_jsonl.snapshot_cursors_for("jl2-n")
        assert "root-j:fork-1" in cursors
    finally:
        _reset_modules()


# ============================================================ _handle_rpc_response


def test_handle_rpc_response_resolves_ok_and_error():
    conn = _conn("rpc-n")

    async def _scenario():
        loop = asyncio.get_running_loop()
        fut_ok = loop.create_future()
        fut_err = loop.create_future()
        conn.pending_rpcs["req-1"] = fut_ok
        conn.pending_rpcs["req-2"] = fut_err
        node_link._handle_rpc_response("rpc-n", {"request_id": "req-1", "ok": True, "result": {"v": 9}})
        node_link._handle_rpc_response("rpc-n", {"request_id": "req-2", "ok": False, "error": "boom"})
        return fut_ok, fut_err

    try:
        fut_ok, fut_err = _run(_scenario())
        assert fut_ok.result() == {"v": 9}
        try:
            fut_err.result()
            assert False
        except RuntimeError as exc:
            assert "boom" in str(exc)
    finally:
        _reset_modules()


def test_handle_rpc_response_noop_for_missing_conn_request_or_done():
    conn = _conn("rpc2-n")

    async def _scenario():
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        done.set_result("already")
        conn.pending_rpcs["d"] = done
        # No connection -> noop.
        node_link._handle_rpc_response("ghost", {"request_id": "d", "ok": True})
        # No request_id -> noop.
        node_link._handle_rpc_response("rpc2-n", {"ok": True})
        # Already-done future -> noop.
        node_link._handle_rpc_response("rpc2-n", {"request_id": "d", "ok": True, "result": "x"})
        # Unknown request_id -> noop.
        node_link._handle_rpc_response("rpc2-n", {"request_id": "missing", "ok": True})

    try:
        _run(_scenario())  # no raise; done future not overwritten
    finally:
        _reset_modules()


# ============================================================ outbound send API


def test_send_spawn_run_offline_and_version_gate_and_happy():
    # Offline -> NodeOffline.
    try:
        _run(node_link.send_spawn_run("ghost", {"run_id": "r"}))
        assert False
    except node_link.NodeOffline:
        pass

    # Connected but not version-ready -> assert_node_version_ready raises.
    conn = _conn("sp-n", runtime_ready=False)
    try:
        try:
            _run(node_link.send_spawn_run("sp-n", {"run_id": "r"}))
            assert False
        except Exception:
            pass
        # version-ready -> sends.
        conn.runtime_ready = True

        async def _scenario():
            await node_link.send_spawn_run("sp-n", {"run_id": "r"})

        _run(_scenario())
        assert conn.ws.sent[-1]["type"] == "spawn_run"
        assert conn.ws.sent[-1]["run_id"] == "r"
    finally:
        _reset_modules()


def test_send_rehook_run_offline_and_happy():
    try:
        _run(node_link.send_rehook_run("ghost", "r1"))
        assert False
    except node_link.NodeOffline:
        pass
    conn = _conn("rh-n", runtime_ready=True)

    async def _scenario():
        await node_link.send_rehook_run("rh-n", "r1")

    try:
        _run(_scenario())
        assert conn.ws.sent[-1] == {"type": "rehook_run", "run_id": "r1"}
    finally:
        _reset_modules()


def test_send_cancel_run_offline_false_happy_true_send_error_false():
    assert _run(node_link.send_cancel_run("ghost", "r1")) is False
    conn = _conn("cn-n")

    async def _ok():
        return await node_link.send_cancel_run("cn-n", "r1")

    try:
        assert _run(_ok()) is True
        assert conn.ws.sent[-1] == {"type": "cancel_run", "run_id": "r1"}

    finally:
        _reset_modules()

    # send_json raises -> False.
    class _FailWS(_FakeWS):
        async def send_json(self, obj):
            raise OSError("gone")

    conn2 = node_store.NodeConnection(
        spec=_spec("cn2-n"), ws=_FailWS(), connected_at=0.0, last_seen=0.0)

    node_store._conns["cn2-n"] = conn2

    async def _fail():
        return await node_link.send_cancel_run("cn2-n", "r1")

    try:
        assert _run(_fail()) is False
    finally:
        _reset_modules()


def test_send_restart_offline_and_happy():
    try:
        _run(node_link.send_restart("ghost"))
        assert False
    except node_link.NodeOffline:
        pass
    conn = _conn("rs-n")

    async def _scenario():
        await node_link.send_restart("rs-n")

    try:
        _run(_scenario())
        assert conn.ws.sent[-1] == {"type": "restart"}
    finally:
        _reset_modules()


# ============================================================ rpc_call


def test_rpc_call_offline_secure_gate_and_happy():
    # Offline -> NodeOffline.
    try:
        _run(node_link.rpc_call("ghost", "ls"))
        assert False
    except node_link.NodeOffline:
        pass

    # Secure transport required + non-secure (ws, non-loopback) -> RuntimeError.
    conn = _conn("rpc-sec", ws=_FakeWS(scheme="ws", host="203.0.113.9"))
    try:
        try:
            _run(node_link.rpc_call("rpc-sec", "ls", secure_transport_required=True))
            assert False
        except RuntimeError as exc:
            assert "WSS or loopback" in str(exc)
    finally:
        _reset_modules()

    # Happy path: request sent, response resolves the future.
    conn = _conn("rpc-ok")

    async def _scenario():
        task = asyncio.ensure_future(node_link.rpc_call("rpc-ok", "ls", {"p": 1}, timeout=2.0))
        await asyncio.sleep(0.02)
        # The request frame was sent.
        assert conn.ws.sent[-1]["type"] == "rpc_request"
        rid = conn.ws.sent[-1]["request_id"]
        # Simulate the node's reply resolving it.
        node_link._handle_rpc_response("rpc-ok", {"request_id": rid, "ok": True, "result": {"v": 1}})
        return await task

    try:
        result = _run(_scenario())
        assert result == {"v": 1}
    finally:
        _reset_modules()


def test_rpc_call_version_ready_gate():
    conn = _conn("rpc-vr", runtime_ready=False)

    async def _scenario():
        await node_link.rpc_call("rpc-vr", "ls", version_ready_required=True)

    try:
        try:
            _run(_scenario())
            assert False
        except Exception:
            pass
    finally:
        _reset_modules()


def test_rpc_call_timeout_pops_pending_and_raises():
    conn = _conn("rpc-to")

    async def _scenario():
        try:
            await node_link.rpc_call("rpc-to", "ls", timeout=0.01)
            assert False
        except asyncio.TimeoutError:
            pass

    try:
        _run(_scenario())
        assert conn.pending_rpcs == {}  # popped on timeout
    finally:
        _reset_modules()


# ============================================================ node_runtime_operations


class _FakeRequest:
    def __init__(self, headers, body):
        self._headers = headers
        self._body = body

    @property
    def headers(self):
        return self._headers

    async def json(self):
        return self._body


def test_runtime_operations_rejects_bad_authority():
    # No node id header -> 403.
    req = _FakeRequest({}, {"x": 1})

    async def _scenario():
        try:
            await node_link.node_runtime_operations(req)
            assert False
        except Exception as exc:
            return exc

    exc = _run(_scenario())
    assert getattr(exc, "status_code", None) == 403


def test_runtime_operations_rejects_non_dict_body():
    # Registered node + valid secret, but a non-dict body -> 400.
    rec = _seed_registry("rt-n", secret="rt-secret")
    node_store._conns["rt-n"] = node_store.NodeConnection(
        spec=node_registry_store.spec_from_record(rec), ws=_FakeWS(),
        connected_at=0.0, last_seen=0.0)
    req = _FakeRequest(
        {"X-Better-Agent-Node-ID": "rt-n", "Authorization": "Bearer rt-secret"},
        ["not", "a", "dict"],
    )

    async def _scenario():
        try:
            await node_link.node_runtime_operations(req)
            assert False
        except Exception as exc:
            return exc

    try:
        exc = _run(_scenario())
        assert getattr(exc, "status_code", None) == 400
    finally:
        _reset_modules()


def test_runtime_operations_routes_to_handler_with_node_id():
    rec = _seed_registry("rt2-n", secret="rt-secret")
    node_store._conns["rt2-n"] = node_store.NodeConnection(
        spec=node_registry_store.spec_from_record(rec), ws=_FakeWS(),
        connected_at=0.0, last_seen=0.0)
    import types
    fake_api = types.ModuleType("runtime_operation_api")
    handled = []

    async def _handle(payload):
        handled.append(payload)
        return {"ok": True}

    fake_api.handle = _handle
    saved = sys.modules.get("runtime_operation_api")
    sys.modules["runtime_operation_api"] = fake_api
    req = _FakeRequest(
        {"X-Better-Agent-Node-ID": "rt2-n", "Authorization": "Bearer rt-secret"},
        {"op": "reload"},
    )

    async def _scenario():
        return await node_link.node_runtime_operations(req)

    try:
        out = _run(_scenario())
        assert out == {"ok": True}
        assert handled[0]["node_id"] == "rt2-n"  # node_id injected
        assert handled[0]["op"] == "reload"
    finally:
        if saved is not None:
            sys.modules["runtime_operation_api"] = saved
        else:
            sys.modules.pop("runtime_operation_api", None)
        _reset_modules()


# ============================================================ self-runner

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"  {name} ...", end=" ")
            fn()
            print("ok")
    print("all node_link tests passed")

"""Fix C — opened-vs-warm WS subscriptions.

Locks the subscribe-frame `priority` wire contract:
  * optional `priority: "opened" | "warm"`; absent field = "opened";
    any other value is invalid and rejected fail-closed (no coercion).
  * The hub registry records per-subscriber priority and exposes it
    (`subscriber_priorities`); warm subscribers receive the same
    change-only `chat_tree_delta` frames (opened ordered first, warm
    never dropped).
  * bff_server's browser->upstream pump registers valid priorities in
    the hub, skips registration for invalid ones, and still forwards
    every frame upstream (runtime is the single rejection authority).
  * main.py's runtime subscribe branch validates the priority BEFORE any
    registration side effect, rejects via the existing frame-validation
    error path, and gates startup-recovery promotion on "opened".

Run with:
    cd backend && .venv/bin/python scripts/test_ws_subscribe_priority.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ws-sub-priority-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import bff_server  # noqa: E402
from bff_current_turn_cache import TurnDelta  # noqa: E402
from bff_current_turn_feed import _default_delta_publisher  # noqa: E402
from bff_event_hub import BffEventHub, hub  # noqa: E402
from i18n import t  # noqa: E402
from ws_subscription_contract import (  # noqa: E402
    PRIORITY_OPENED,
    PRIORITY_WARM,
    resolve_subscribe_priority,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _FakeWS:
    """Minimal stand-in for a starlette WebSocket on the hub side."""

    def __init__(self, log: list[tuple[str, str]], name: str) -> None:
        self._log = log
        self._name = name

    async def send_text(self, data: str) -> None:
        self._log.append((self._name, data))


async def test_contract_resolution() -> bool:
    ok = True
    cases = [
        ({}, PRIORITY_OPENED),
        ({"priority": "opened"}, PRIORITY_OPENED),
        ({"priority": "warm"}, PRIORITY_WARM),
        ({"priority": "hot"}, None),
        ({"priority": None}, None),
        ({"priority": 1}, None),
        ({"priority": ""}, None),
    ]
    for frame, expected in cases:
        got = resolve_subscribe_priority(frame)
        if got != expected:
            print(f"  resolve_subscribe_priority({frame!r}) = {got!r}, expected {expected!r}")
            ok = False
    return ok


async def test_hub_registry_and_warm_delivery() -> bool:
    local_hub = BffEventHub()
    log: list[tuple[str, str]] = []
    opened_conn = local_hub.attach(_FakeWS(log, "opened"))
    warm_conn = local_hub.attach(_FakeWS(log, "warm"))
    sid = "sid-registry"

    # Subscribe warm FIRST so opened-first delivery ordering is proven by
    # sort, not by insertion order.
    local_hub.subscribe(warm_conn, sid, priority=PRIORITY_WARM)
    local_hub.subscribe(opened_conn, sid)  # default = opened

    registry = local_hub.subscriber_priorities(sid)
    expected = {warm_conn.id: PRIORITY_WARM, opened_conn.id: PRIORITY_OPENED}
    if registry != expected:
        print(f"  registry {registry!r} != expected {expected!r}")
        return False

    await local_hub.publish_session(sid, {"type": "chat_tree_delta", "data": {"x": 1}})
    names = [name for name, _ in log]
    if sorted(names) != ["opened", "warm"]:
        print(f"  warm subscriber dropped from fanout: delivered to {names!r}")
        return False
    if names[0] != "opened":
        print(f"  opened subscriber not prioritized first: order {names!r}")
        return False

    try:
        local_hub.subscribe(opened_conn, sid, priority="hot")
    except ValueError:
        pass
    else:
        print("  hub.subscribe accepted invalid priority 'hot' (must raise)")
        return False
    if local_hub.subscriber_priorities(sid) != expected:
        print("  invalid subscribe mutated the registry")
        return False

    local_hub.unsubscribe(warm_conn, sid)
    if local_hub.subscriber_priorities(sid) != {opened_conn.id: PRIORITY_OPENED}:
        print("  unsubscribe did not remove the warm subscriber")
        return False
    local_hub.detach(opened_conn)
    if local_hub.subscriber_priorities(sid) != {}:
        print("  detach did not clear the registry")
        return False
    return True


async def test_warm_receives_chat_tree_delta_via_feed_publisher() -> bool:
    """A warm subscriber on the GLOBAL hub receives the exact
    `chat_tree_delta` frames `bff_current_turn_feed` publishes."""
    log: list[tuple[str, str]] = []
    conn = hub.attach(_FakeWS(log, "warm"))
    root_id = "root-warm-feed"
    try:
        hub.subscribe(conn, root_id, priority=PRIORITY_WARM)
        delta = TurnDelta(items=[{"id": "i1"}], lookup={"i1": {"k": "v"}})
        await _default_delta_publisher(root_id, "turn-1", "streaming", delta)
    finally:
        hub.detach(conn)
    if len(log) != 1:
        print(f"  expected 1 delivered frame, got {len(log)}")
        return False
    frame = json.loads(log[0][1])
    expected = {
        "type": "chat_tree_delta",
        "data": {
            "app_session_id": root_id,
            "turn_id": "turn-1",
            "phase": "streaming",
            "items": [{"id": "i1"}],
            "lookup": {"i1": {"k": "v"}},
        },
    }
    if frame != expected:
        print(f"  frame {frame!r} != expected {expected!r}")
        return False
    return True


class _Url:
    def __init__(self) -> None:
        self.path = "/ws/chat"
        self.query = ""
        self.scheme = "ws"


class _ScriptedBrowserWS:
    """Browser socket that plays a scripted frame sequence, then disconnects."""

    def __init__(self, frames: list[str]) -> None:
        self.headers = {"host": "127.0.0.1:18765"}
        self.url = _Url()
        self.client = None
        self.close_code = None
        self._frames = list(frames)

    async def accept(self) -> None:
        return None

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "text": self._frames.pop(0)}
        return {"type": "websocket.disconnect"}

    async def send_text(self, _data: str) -> None:
        return None

    async def send_bytes(self, _data: bytes) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


class _RecordingUpstream:
    """Upstream that records forwarded frames and snapshots the hub
    registry at each forward (browser_to_upstream subscribes BEFORE it
    forwards, so the snapshot reflects that frame's effect)."""

    def __init__(self, watch_sids: list[str]) -> None:
        self.close_code = None
        self.closed = False
        self.sent: list[str] = []
        self.snapshots: list[dict[str, dict[str, str]]] = []
        self._watch_sids = watch_sids
        self._never = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._never.wait()
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent.append(data)
        self.snapshots.append({
            sid: hub.subscriber_priorities(sid) for sid in self._watch_sids
        })

    async def close(self) -> None:
        self.closed = True


class _FakeLease:
    def __init__(self) -> None:
        self.descriptor = {"kind": "uds", "path": "/tmp/ba-test-ws-sub-priority.sock"}

    async def release(self) -> None:
        return None


async def test_bff_proxy_records_priority_and_fails_closed() -> bool:
    sids = ["sid-warm", "sid-open", "sid-bad"]
    frames = [
        json.dumps({"type": "subscribe", "app_session_id": "sid-warm", "priority": "warm"}),
        json.dumps({"type": "subscribe", "app_session_id": "sid-open"}),
        json.dumps({"type": "subscribe", "app_session_id": "sid-bad", "priority": "hot"}),
        json.dumps({"type": "unsubscribe", "app_session_id": "sid-warm"}),
    ]
    ws = _ScriptedBrowserWS(frames)
    upstream = _RecordingUpstream(sids)

    async def _fake_acquire():
        return _FakeLease()

    async def _fake_unix_connect(*_args, **_kwargs):
        return upstream

    orig_acquire = bff_server.runtime_upstream.acquire
    orig_unix_connect = bff_server.websockets.unix_connect
    bff_server.runtime_upstream.acquire = _fake_acquire  # type: ignore[assignment]
    bff_server.websockets.unix_connect = _fake_unix_connect  # type: ignore[assignment]
    try:
        await bff_server.proxy_ws(ws, "chat")
    finally:
        bff_server.runtime_upstream.acquire = orig_acquire  # type: ignore[assignment]
        bff_server.websockets.unix_connect = orig_unix_connect  # type: ignore[assignment]

    if len(upstream.sent) != 4:
        print(f"  expected all 4 frames forwarded upstream, got {len(upstream.sent)}")
        return False
    snap_warm, snap_open, snap_bad, snap_unsub = upstream.snapshots
    if list(snap_warm["sid-warm"].values()) != [PRIORITY_WARM]:
        print(f"  warm subscribe not recorded: {snap_warm!r}")
        return False
    if list(snap_open["sid-open"].values()) != [PRIORITY_OPENED]:
        print(f"  default subscribe not recorded as opened: {snap_open!r}")
        return False
    if snap_bad["sid-bad"] != {}:
        print(f"  invalid priority was registered (must fail closed): {snap_bad!r}")
        return False
    if snap_unsub["sid-warm"] != {}:
        print(f"  unsubscribe did not clear the warm subscription: {snap_unsub!r}")
        return False
    for sid in sids:
        if hub.subscriber_priorities(sid) != {}:
            print(f"  registry leaked after detach for {sid}")
            return False
    return True


async def test_runtime_subscribe_branch_validates_and_gates() -> bool:
    source = (Path(_BACKEND) / "main.py").read_text(encoding="utf-8")
    ok = True
    subscribe_at = source.find('if msg_type == "subscribe":')
    if subscribe_at < 0:
        print("  subscribe branch not found in main.py")
        return False
    branch = source[subscribe_at:subscribe_at + 4000]
    validate_at = branch.find("resolve_subscribe_priority(msg)")
    reject_at = branch.find('t("error.ws_invalid_subscribe_priority")')
    register_at = branch.find("_register(sub_sid")
    promote_at = branch.find("request_session_priority")
    gate_at = branch.find("if sub_priority == PRIORITY_OPENED:")
    if validate_at < 0:
        print("  subscribe branch does not resolve the frame priority")
        ok = False
    if reject_at < 0:
        print("  subscribe branch has no invalid-priority rejection via t()")
        ok = False
    if register_at >= 0 and validate_at >= 0 and not (validate_at < reject_at < register_at):
        print("  priority validation/rejection must precede registration")
        ok = False
    if gate_at < 0 or promote_at < 0 or not (gate_at < promote_at):
        print("  startup-recovery promotion is not gated on opened priority")
        ok = False
    if t("error.ws_invalid_subscribe_priority") == "error.ws_invalid_subscribe_priority":
        print("  i18n key error.ws_invalid_subscribe_priority is missing")
        ok = False
    if t("error.ws_invalid_subscribe_priority", "he") == "error.ws_invalid_subscribe_priority":
        print("  he i18n key error.ws_invalid_subscribe_priority is missing")
        ok = False
    return ok


TESTS = [
    ("subscribe priority contract: absent=opened, invalid=rejected", test_contract_resolution),
    ("hub registry records + exposes priority; warm delivered, opened first", test_hub_registry_and_warm_delivery),
    ("warm subscriber receives chat_tree_delta via feed publisher", test_warm_receives_chat_tree_delta_via_feed_publisher),
    ("bff proxy registers priority, fails closed on invalid", test_bff_proxy_records_priority_and_fails_closed),
    ("runtime subscribe branch validates, rejects, gates promotion", test_runtime_subscribe_branch_validates_and_gates),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = asyncio.run(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            ok = False
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())

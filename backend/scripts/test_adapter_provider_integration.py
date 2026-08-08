#!/usr/bin/env python3
"""End-to-end coverage for Package D (ADR 0007): legacy REST mutation ->
D1 bus fact -> `/ws/v2/surface` typed push frame, D2's installable-catalog
REST route, and D4's provider-intent WS round trip. Exercises the REAL
`providers_api.py`/`runtime_profiles_api.py`/`adapter_api.py` routers
together (not `main.py`'s full app — no auth_gate middleware/extension
boot needed for this surface; same lighter-weight recipe
`backend/scripts/test_adapter_api.py`'s `_build_app()` already uses for
`/ws/v2/surface`), against an isolated BA home so no real provider config
is ever touched.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_provider_integration.py -q
    PYTHONPATH=. python3 backend/scripts/test_adapter_provider_integration.py
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-provider-integration-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import auth  # noqa: E402
import adapter_api  # noqa: E402
import providers_api  # noqa: E402
import runtime_profiles_api  # noqa: E402
from backend.adapters.provider_adapter import ProviderConfigSurfaceAdapter  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

_TEST_TOKEN = "test-bearer-token"


async def _noop_broadcast(event: str, payload: dict) -> None:
    return None


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(providers_api.router)
    app.include_router(runtime_profiles_api.router)
    app.include_router(adapter_api.router)
    return app


def _wire() -> None:
    providers_api.configure(_noop_broadcast)
    runtime_profiles_api.configure(_noop_broadcast)
    adapter = ProviderConfigSurfaceAdapter()
    adapter.bind()
    adapter_api.configure(providers=adapter)
    auth.verify_token = lambda tok: {"username": "tester"} if tok == _TEST_TOKEN else None


def _drain_until(ws, predicate, limit: int = 20) -> dict:
    for _ in range(limit):
        frame = ws.receive_json()
        if predicate(frame):
            return frame
    raise AssertionError(f"predicate never matched within {limit} frames")


def _collect(ws, count: int) -> list[dict]:
    """Read exactly `count` frames into a buffer — use instead of two
    sequential `_drain_until` calls when a single mutation is expected to
    publish more than one frame: `_drain_until` discards every frame it
    skips past, so a second call can never see one an earlier call already
    read over."""
    return [ws.receive_json() for _ in range(count)]


def _create_provider(client: TestClient, *, kind: str = "claude", mode: str = "subscription") -> dict:
    resp = client.post("/api/providers", json={"name": f"T-{uuid.uuid4().hex[:6]}", "kind": kind, "mode": mode})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# D2: installable catalog REST route
# ---------------------------------------------------------------------------

def test_installable_catalog_route_returns_every_template() -> None:
    _wire()
    client = TestClient(_build_app())
    resp = client.get("/api/v2/surface/providers/installable")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "ok"
    kinds = {d["kind"] for d in body["value"]}
    assert "claude" in kinds and "codex" in kinds and "custom-openai" in kinds
    for descriptor in body["value"]:
        assert "form_schema" in descriptor and "defaults" in descriptor
        serialized = str(descriptor)
        # Secret fields never carry a literal secret value in the catalog.
        assert "ollama" != descriptor.get("defaults", {}).get("api_key", "") or descriptor["kind"] == "ollama"


# ---------------------------------------------------------------------------
# D1: legacy REST mutation -> bus fact -> /ws/v2/surface push frame
# ---------------------------------------------------------------------------

def test_create_provider_pushes_provider_upsert_over_v2_feed() -> None:
    _wire()
    client = TestClient(_build_app())
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        created = _create_provider(client)
        frame = _drain_until(
            ws, lambda f: f.get("type") == "provider_upsert" and f["descriptor"]["provider_id"] == created["id"],
        )
        assert frame["descriptor"]["config_state"] == "active"
        assert frame["descriptor"]["auth_flows"] == ["oauth_subscription"]


def test_suspend_provider_pushes_credential_state_frame() -> None:
    _wire()
    client = TestClient(_build_app())
    created = _create_provider(client)
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        resp = client.post(
            f"/api/providers/{created['id']}/suspended",
            json={
                "suspended": True,
                "expected_generation": created["generation"],
                "expected_revision": created["revision"],
            },
        )
        assert resp.status_code == 200, resp.text
        # The same suspend fires TWO facts (structural — revision bumped —
        # and credential-state), so both frames land on this feed from one
        # mutation; read them together rather than draining twice (see
        # `_collect`'s docstring for why sequential `_drain_until` calls
        # would lose the first one).
        frames = _collect(ws, 2)
        credential_frames = [f for f in frames if f.get("type") == "credential_state"]
        upsert_frames = [f for f in frames if f.get("type") == "provider_upsert"]
        assert len(credential_frames) == 1, frames
        assert credential_frames[0]["provider_id"] == created["id"]
        assert credential_frames[0]["config_state"] == "suspended"
        assert len(upsert_frames) == 1, frames
        assert upsert_frames[0]["descriptor"]["provider_id"] == created["id"]
        assert upsert_frames[0]["descriptor"]["config_state"] == "suspended"


def test_refresh_models_pushes_model_catalog_changed_frame_when_a_diff_exists() -> None:
    _wire()
    client = TestClient(_build_app())
    created = _create_provider(client, kind="fugu")  # curated catalog, no external refresh — diff is deterministic
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        # fugu has a curated, static FUGU_MODELS catalog — a refresh is a
        # true no-op (see providers_api._refresh_provider_models), so this
        # asserts the ABSENCE of a spurious frame instead: the D1 fact is
        # only published behind a real diff, exactly like the legacy
        # `models_catalog_changed` WS event it decomposes.
        resp = client.post(f"/api/providers/{created['id']}/models/refresh")
        assert resp.status_code == 202, resp.text
        ws.send_json({"intent": {
            "kind": "refresh_models", "intent_id": "noop-check", "session_id": None,
            "provider_id": created["id"],
        }})
        ack = _drain_until(ws, lambda f: f.get("type") in ("intent_accepted", "intent_rejected"))
        assert ack["type"] == "intent_accepted"


def test_runtime_profile_create_pushes_model_catalog_changed_for_its_provider() -> None:
    _wire()
    client = TestClient(_build_app())
    created = _create_provider(client)
    runner = created["runner_options"][-1]
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        resp = client.post("/api/runtime-profiles", json={"provider_id": created["id"], "runner": runner})
        assert resp.status_code == 200, resp.text
        frame = _drain_until(
            ws, lambda f: f.get("type") == "model_catalog_changed" and f["provider_id"] == created["id"],
        )
        assert frame["provider_id"] == created["id"]


def test_runtime_profile_create_pushes_runtime_profiles_changed_frame_with_full_snapshot() -> None:
    """Closure 3 (RuntimeProfile v2 parity): the dedicated
    `runtime_profiles_changed` frame carries the full snapshot
    (`default_runtime_profile_id`/`profiles` incl. the new one), not just
    the `model_catalog_changed` signal asserted above."""
    _wire()
    client = TestClient(_build_app())
    created = _create_provider(client)
    runner = created["runner_options"][-1]
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        resp = client.post("/api/runtime-profiles", json={"provider_id": created["id"], "runner": runner})
        assert resp.status_code == 200, resp.text
        profile_id = resp.json()["id"]
        frame = _drain_until(ws, lambda f: f.get("type") == "runtime_profiles_changed")
        snapshot = frame["snapshot"]
        assert snapshot["default_runtime_profile_id"]
        assert any(p["runtime_profile_id"] == profile_id for p in snapshot["profiles"])


def test_v2_runtime_profiles_rest_route_returns_full_snapshot() -> None:
    _wire()
    client = TestClient(_build_app())
    created = _create_provider(client)
    resp = client.get("/api/v2/surface/runtime-profiles")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "ok"
    assert "default_runtime_profile_id" in body
    assert "last_models" in body
    assert "deleted_providers" in body
    assert any(p["provider_id"] == created["id"] for p in body["profiles"])


# ---------------------------------------------------------------------------
# D4: provider intents over /ws/v2/surface
# ---------------------------------------------------------------------------

def test_suspend_provider_intent_actually_suspends_through_the_real_mutation_path() -> None:
    _wire()
    client = TestClient(_build_app())
    created = _create_provider(client)
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        ws.send_json({"intent": {
            "kind": "suspend_provider", "intent_id": "intent-suspend-1", "session_id": None,
            "provider_id": created["id"],
        }})
        ack = _drain_until(ws, lambda f: f.get("type") in ("intent_accepted", "intent_rejected"))
        assert ack["type"] == "intent_accepted", ack
        assert ack["intent_id"] == "intent-suspend-1"
        _drain_until(
            ws,
            lambda f: f.get("type") == "provider_upsert" and f["descriptor"]["provider_id"] == created["id"]
            and f["descriptor"]["config_state"] == "suspended",
        )
    # Durable effect, independent of the WS frame: the REST read model
    # agrees the provider is now suspended.
    resp = client.get("/api/providers")
    record = next(p for p in resp.json()["providers"] if p["id"] == created["id"])
    assert record["suspended"] is True


def test_create_provider_intent_actually_creates_through_the_real_mutation_path() -> None:
    _wire()
    client = TestClient(_build_app())
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        ws.send_json({"intent": {
            "kind": "create_provider", "intent_id": "intent-create-1", "session_id": None,
            "provider_kind": "claude", "config": {"name": "Via-Intent", "mode": "subscription"},
        }})
        ack = _drain_until(ws, lambda f: f.get("type") in ("intent_accepted", "intent_rejected"))
        assert ack["type"] == "intent_accepted", ack
        _drain_until(
            ws, lambda f: f.get("type") == "provider_upsert" and f["descriptor"]["display"]["label"] == "Via-Intent",
        )


def test_begin_login_intent_over_ws_is_gated_by_the_same_loopback_check_as_the_legacy_route() -> None:
    # starlette's TestClient WS does NOT present as a loopback client
    # (unlike a real desktop browser tab hitting a local backend) — so
    # this is really exercising the security property that matters: a
    # non-loopback caller is rejected BEFORE `ProviderConfigSurfaceAdapter.
    # submit()` is ever reached, exactly like `providers_api.login_provider`
    # rejects a non-loopback REST caller. `_handle_intent`'s dispatch/
    # accept path for `begin_login` (parser + command-port wiring) is
    # covered directly in `backend/scripts/test_adapter_provider.py`
    # (`test_submit_dispatches_every_intent_kind_to_the_command_port`),
    # bypassing this transport-level gate on purpose.
    _wire()
    client = TestClient(_build_app())
    created = _create_provider(client)
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"intent": {
            "kind": "begin_login", "intent_id": "intent-login-1", "session_id": None,
            "provider_id": created["id"], "flow": "oauth_subscription",
        }})
        ack = _drain_until(ws, lambda f: f.get("type") in ("intent_accepted", "intent_rejected"))
        assert ack["type"] == "intent_rejected", ack
        assert ack["code"] == "forbidden"


def test_update_provider_intent_for_missing_provider_pushes_a_late_intent_rejected_frame() -> None:
    """D4 intent error propagation: `update_provider` is admitted
    synchronously (a well-formed payload), but the underlying mutation
    fails once its fire-and-forget coroutine actually runs (unknown
    provider_id -> `providers_api._patch_provider`'s 404) — that failure
    must reach the client as a SECOND, async `intent_rejected` frame over
    the SAME `intent_id`, not vanish into a server-side log line."""
    _wire()
    client = TestClient(_build_app())
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"feeds": ["providers"]})
        ws.send_json({"intent": {
            "kind": "update_provider", "intent_id": "intent-missing-1", "session_id": None,
            "provider_id": "does-not-exist", "config_patch": {"name": "New Name"},
        }})
        admission = ws.receive_json()
        assert admission["type"] == "intent_accepted", admission
        assert admission["intent_id"] == "intent-missing-1"
        rejection = _drain_until(
            ws, lambda f: f.get("type") == "intent_rejected" and f.get("intent_id") == "intent-missing-1",
        )
        assert rejection["code"] == "404"
        assert "provider" in rejection["message"].lower()


def test_unknown_provider_intent_kind_is_rejected_as_malformed() -> None:
    _wire()
    client = TestClient(_build_app())
    with client.websocket_connect(f"/ws/v2/surface?token={_TEST_TOKEN}") as ws:
        ws.send_json({"intent": {"kind": "not_a_real_intent", "intent_id": "bad-1", "session_id": None}})
        ack = ws.receive_json()
        assert ack["type"] == "intent_rejected"
        assert ack["code"] == "malformed_intent"


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [
        (name, fn) for name, fn in vars(module).items()
        if name.startswith("test_") and inspect.isfunction(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)

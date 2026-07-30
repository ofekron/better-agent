#!/usr/bin/env python3
"""Live end-to-end test: the ACTIVE runtime profile drives a real turn.

Isolation: its own `BETTER_AGENT_HOME`, its own activated installation, its
own uvicorn on a free port. Nothing touches the developer's real state.

Cost: gated behind RUN_LLM_TESTS=1 (per-run user permission) and pinned to
the cheapest model of every provider. Providers whose CLI is not installed
are skipped.

The deterministic suites already lock profile CRUD, activation, and
last-model bookkeeping in memory. What only a live run can prove is that an
activated runtime profile — and nothing else — selects what actually
executes:

    activate profile (PATCHed to a cheap default model)
      -> POST /api/sessions with NO provider/model/runner   (profile fills all three)
      -> WS send_message with NO model override             (session stamp wins)
      -> run `input.json` model/provider_id                 (crosses into the subprocess)
      -> a real assistant turn                              (the model answered)

Each provider kind runs the same scenario in one backend; activating the
next provider's profile between iterations is itself the proof that
activation switches what new sessions execute on.

Run with:
    cd backend && RUN_LLM_TESTS=1 .venv/bin/python \
        scripts/integration_test_runtime_profile_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

BA_HOME = _test_home.isolate("bc-int-runtime-profile-")

import _live_turn_harness as harness  # noqa: E402

harness.engage_headless_auth(Path(BA_HOME))

import _test_installation  # noqa: E402
import httpx  # noqa: E402

from live_llm_test_guard import require_live_llm_tests  # noqa: E402

CHEAP_MODELS = harness.cheap_models("BA_RTP_E2E")
# Provider kind -> CLI binary a live turn needs on PATH.
PROVIDER_CLIS = {"claude": "claude", "codex": "codex", "agy": "agy"}
PROMPT = "Reply with exactly the word: pong"


class ProfileDrivenBackend:
    """One isolated backend, driven only through the surfaces the frontend
    uses: runtime-profile REST + selector-less session creation + WS."""

    def __init__(self, client: httpx.AsyncClient, ws_url: str, cookie: str, cwd: str):
        self.client = client
        self.ws_url = ws_url
        self.cookie = cookie
        self.cwd = cwd

    async def _post(self, path: str, payload: dict | None = None) -> dict:
        response = await self.client.post(path, json=payload if payload is not None else {})
        if response.status_code != 200:
            raise RuntimeError(
                f"POST {path} -> HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    async def profiles_snapshot(self) -> dict:
        response = await self.client.get("/api/runtime-profiles")
        if response.status_code != 200:
            raise RuntimeError(
                f"GET /api/runtime-profiles -> HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    async def create_provider(self, kind: str) -> str:
        created = await self._post("/api/providers", {
            "name": f"{kind}-rtp-e2e", "kind": kind, "mode": "subscription",
        })
        return created["id"]

    async def seeded_live_profile(self, provider_id: str) -> dict | None:
        snapshot = await self.profiles_snapshot()
        return next(
            (
                profile
                for profile in snapshot["runtime_profiles"]
                if profile["provider_id"] == provider_id and not profile["deleted_at"]
            ),
            None,
        )

    async def set_profile_default_model(self, profile_id: str, model: str) -> dict:
        response = await self.client.patch(
            f"/api/runtime-profiles/{profile_id}", json={"default_model": model},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"PATCH runtime-profile -> HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    async def activate(self, profile_id: str) -> None:
        await self._post(f"/api/runtime-profiles/{profile_id}/activate", {})

    async def create_bare_session(self, name: str) -> str:
        """No provider_id, no model, no runner — everything the turn needs
        must come from the active runtime profile."""
        return (await self._post("/api/sessions", {
            "name": name, "cwd": self.cwd, "orchestration_mode": "native",
        }))["id"]

    async def turn_without_model(self, sid: str) -> harness.TurnResult:
        return await harness.drive_turn(
            self.ws_url, self.cookie, sid, self.cwd, PROMPT, Path(BA_HOME), model=None,
        )


async def _exercise_provider(backend: ProfileDrivenBackend, kind: str) -> str:
    """Run the whole profile-driven chain for one provider kind.

    Returns "" on success, "skip:<reason>" when the provider itself is
    unavailable, and a failure description otherwise.
    """
    from session_manager import manager as session_manager  # noqa: PLC0415

    cheap = CHEAP_MODELS[kind]
    provider_id = await backend.create_provider(kind)

    profile = await backend.seeded_live_profile(provider_id)
    if profile is None:
        return "provider creation did not seed a live runtime profile"
    harness.ok(f"[{kind}] provider creation seeded live profile {profile['id'][:8]}")

    patched = await backend.set_profile_default_model(profile["id"], cheap)
    if patched.get("default_model") != cheap:
        return f"PATCH default_model -> {patched.get('default_model')!r}, wanted {cheap!r}"

    await backend.activate(profile["id"])
    snapshot = await backend.profiles_snapshot()
    if snapshot.get("default_runtime_profile_id") != profile["id"]:
        return (
            f"activation did not stick: default_runtime_profile_id="
            f"{snapshot.get('default_runtime_profile_id')!r}"
        )
    harness.ok(f"[{kind}] profile activated with default model {cheap!r}")

    sid = await backend.create_bare_session(f"rtp-{kind}")
    stored = session_manager.get(sid) or {}
    stamped = (
        stored.get("provider_id"),
        stored.get("model"),
        stored.get("runner"),
    )
    expected = (provider_id, cheap, profile["runner"])
    if stamped != expected:
        return f"bare session stamped {stamped}, active profile implies {expected}"
    harness.ok(f"[{kind}] selector-less session stamped from the active profile")

    turn = await backend.turn_without_model(sid)
    reason = harness.run_failure(harness.run_dir_for(Path(BA_HOME), sid)) or "; ".join(
        turn.errors
    )
    if reason:
        if harness.provider_unavailable(reason):
            return f"skip:{reason}"
        return f"turn failed: {reason}"
    if not turn.text:
        return "the model said nothing on the wire"
    harness.ok(f"[{kind}] real turn answered: {turn.text[:40]!r}")

    run_dir = harness.run_dir_for(Path(BA_HOME), sid)
    if run_dir is None:
        return "no run dir carried this session id"
    spawned = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    if spawned.get("model") != cheap:
        return f"run input.json model={spawned.get('model')!r}, profile says {cheap!r}"
    if spawned.get("provider_id") != provider_id:
        return (
            f"run input.json provider_id={spawned.get('provider_id')!r}, "
            f"profile says {provider_id!r}"
        )
    harness.ok(f"[{kind}] spawned runner received the profile's model and provider")

    after = session_manager.get(sid) or {}
    if (after.get("provider_id"), after.get("runner")) != (provider_id, profile["runner"]):
        return (
            f"stamps drifted after the turn: provider={after.get('provider_id')!r} "
            f"runner={after.get('runner')!r}"
        )
    harness.ok(f"[{kind}] profile-derived stamps intact after the turn")
    return ""


async def main() -> int:
    try:
        return await _run()
    finally:
        # The home is torn down here rather than beside the server so an
        # early failure (skip, activation, a server that never binds) still
        # leaves nothing behind.
        shutil.rmtree(BA_HOME, ignore_errors=True)


async def _run() -> int:
    if not require_live_llm_tests("runtime-profile live e2e"):
        return 0
    runnable = [k for k, cli in PROVIDER_CLIS.items() if shutil.which(cli)]
    if not runnable:
        print("SKIP — no provider CLI (claude/codex/agy) on PATH")
        return 0

    print(f"BETTER_AGENT_HOME = {BA_HOME}")
    _test_installation.activate(Path(BA_HOME))

    port = harness.free_port()
    server = harness.BackgroundUvicorn("main:app", port)
    server.start()
    cwd = tempfile.mkdtemp(prefix="bc-int-runtime-profile-cwd-")
    cookie = harness.mint_session_cookie()
    failures = 0

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=300,
            cookies={"better_agent_session": cookie},
        ) as client:
            response = await client.get("/api/auth/me")
            if response.status_code != 200:
                harness.fail("auth probe", f"HTTP {response.status_code}: {response.text[:200]}")
                return 1
            harness.ok("auth cookie accepted")

            backend = ProfileDrivenBackend(
                client, f"ws://127.0.0.1:{port}/ws/chat", cookie, cwd,
            )
            for kind in runnable:
                try:
                    verdict = await _exercise_provider(backend, kind)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    verdict = f"{type(exc).__name__}: {exc}"
                if verdict.startswith("skip:"):
                    print(f"SKIP  [{kind}] provider unavailable: {verdict[5:]}")
                elif verdict:
                    harness.fail(f"[{kind}] runtime-profile chain", verdict)
                    failures += 1
    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)

    print()
    if failures:
        print(f"\033[91m{failures} FAILURE(S)\033[0m")
        return 1
    print("\033[92mALL PASS — the active runtime profile drives the real turn\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

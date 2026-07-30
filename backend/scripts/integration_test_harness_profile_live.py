#!/usr/bin/env python3
"""Live end-to-end test: a harness profile shapes what the model actually gets.

Isolation: its own `BETTER_AGENT_HOME`, its own activated installation, its
own uvicorn on a free port. Nothing touches the developer's real state.

Model: the Antigravity (`agy`) CLI on its cheapest tier, so proving the
profile reaches a real turn never spends subscription usage on a capable
model.

The deterministic suites already lock resolution and projection in memory.
What only a live run can prove is the whole chain:

    profile override
      -> run `input.json`            (crosses into the spawned subprocess)
      -> overlay `settings.json`     (the MCP surface the vendor CLI loads)
      -> `agy_cli.log` appDataDir    (the CLI confirms it read that overlay)
      -> a real assistant turn       (the model answered under that surface)

Two sessions run the same prompt against the same provider and model. One is
on Default, one is on a named profile that switches off a single extension
MCP server. The profile is the only difference, so a difference in the tool
surface the CLI loaded is caused by the profile and nothing else.

Run with:
    cd backend && RUN_LLM_TESTS=1 .venv/bin/python \
        scripts/integration_test_harness_profile_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

BA_HOME = _test_home.isolate("bc-int-harness-profile-")

import _live_turn_harness as harness  # noqa: E402

harness.engage_headless_auth(Path(BA_HOME))

import _test_installation  # noqa: E402
import httpx  # noqa: E402

from live_llm_test_guard import require_live_llm_tests  # noqa: E402

# Cheapest Antigravity tier — see provider_agy.AGY_MODELS.
CHEAP_MODEL = "Gemini 3.5 Flash (Low)"
# A builtin tool the profile turns off. Its effect is asserted where it is
# observable — the payload handed to the spawned runner.
DISABLED_TOOL = "ask"
PROMPT = "Reply with exactly the word: pong"
# agy writes its CLI home under the run dir; this is the path the vendor CLI
# reads settings (and therefore its MCP servers) from.
CLI_SETTINGS = Path("agy-home/.gemini/antigravity-cli/settings.json")
_APP_DATA_DIR = re.compile(r"appDataDir=(\S+)")

_ok = harness.ok
_fail = harness.fail


def _run_dir_for(session_id: str) -> Path | None:
    return harness.run_dir_for(Path(BA_HOME), session_id)


def _cli_mcp_servers(run_dir: Path) -> set[str]:
    """MCP server names in the overlay settings the vendor CLI loads."""
    settings = run_dir / CLI_SETTINGS
    if not settings.is_file():
        return set()
    try:
        loaded = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set((loaded.get("mcpServers") or {}).keys())


def _cli_app_data_dir(run_dir: Path) -> str:
    """The config directory the vendor CLI reports it actually used."""
    log = run_dir / "agy_cli.log"
    if not log.is_file():
        return ""
    match = _APP_DATA_DIR.search(log.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else ""


def _first_extension_mcp_pair(
    descriptor: dict, present: set[str]
) -> tuple[str, str] | None:
    """An (extension id, MCP server) pair the run actually loaded, so the
    profile switches off something the model demonstrably had."""
    for extension in descriptor.get("extensions") or []:
        for group in extension.get("groups") or []:
            if group.get("id") != "mcp_servers":
                continue
            for item in group.get("items") or []:
                name = str(item.get("name") or "")
                if name in present:
                    return str(extension.get("id") or ""), name
    return None


async def _speak(live: "LiveHarness", sid: str) -> tuple[str, str]:
    """Run one turn. Returns (what the model said, why it did not speak)."""
    turn = await live.turn(sid)
    reason = harness.run_failure(_run_dir_for(sid)) or "; ".join(turn.errors)
    if reason:
        return "", reason
    if not turn.text:
        return "", "the model said nothing on the wire"
    return turn.text, ""


async def _await_governed_default(resolver, projection, budget: float = 180.0) -> set[str]:
    """The extensions Default scopes, once the harness has one to scope.

    Builtin extensions install during startup; a profile resolved before
    that finishes legitimately governs nothing. Returns the governed ids, or
    an empty set if the harness never came up inside the budget.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        resolver.invalidate_cache()
        governed = projection.selected_extension_ids(
            {"resolved_harness_run_config": resolver.resolve_for_session({})}
        )
        if governed:
            return governed
        await asyncio.sleep(0.5)
    return set()


class LiveHarness:
    """One isolated backend, driven the way the frontend drives it."""

    def __init__(self, client: httpx.AsyncClient, ws_url: str, cookie: str, cwd: str):
        self.client = client
        self.ws_url = ws_url
        self.cookie = cookie
        self.cwd = cwd
        self.provider_id = ""

    async def _post(self, path: str, payload: dict | None = None) -> dict:
        response = await self.client.post(path, json=payload if payload is not None else {})
        if response.status_code != 200:
            raise RuntimeError(f"POST {path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    async def use_cheap_provider(self) -> None:
        created = await self._post("/api/providers", {
            "name": "Antigravity-IT", "kind": "agy", "mode": "subscription",
            "default_model": CHEAP_MODEL,
        })
        self.provider_id = created["id"]
        profiles = (await self.client.get("/api/runtime-profiles")).json()
        target = next(
            (
                profile
                for profile in profiles["runtime_profiles"]
                if profile["provider_id"] == self.provider_id
                and not profile["deleted_at"]
            ),
            None,
        )
        if target is None:
            providers = (await self.client.get("/api/providers")).json()["providers"]
            record = next(p for p in providers if p["id"] == self.provider_id)
            target = (await self._post("/api/runtime-profiles", {
                "provider_id": self.provider_id,
                "runner": record["runner_options"][0],
            }))
        await self._post(f"/api/runtime-profiles/{target['id']}/activate", {})

    async def session_on(self, name: str, profile: dict | None = None) -> str:
        payload = {
            "name": name, "model": CHEAP_MODEL, "cwd": self.cwd,
            "orchestration_mode": "native", "provider_id": self.provider_id,
        }
        if profile:
            payload["harness_profile_id"] = profile["id"]
        return (await self._post("/api/sessions", payload))["id"]

    async def turn(self, sid: str) -> harness.TurnResult:
        return await harness.drive_turn(
            self.ws_url, self.cookie, sid, self.cwd, PROMPT, Path(BA_HOME),
            model=CHEAP_MODEL,
        )

    async def profile_disabling(self, name: str, writes: list[dict]) -> dict:
        profile = await self._post("/api/harness-profiles", {"name": name})
        response = await self.client.patch(
            f"/api/harness-profiles/{profile['id']}/fields",
            json={"revision": profile["revision"], "writes": writes},
        )
        if response.status_code != 200:
            raise RuntimeError(f"field write -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json()


async def main() -> int:
    try:
        return await _run()
    finally:
        # The home is torn down here rather than beside the server so an
        # early failure (skip, activation, a server that never binds) still
        # leaves nothing behind.
        shutil.rmtree(BA_HOME, ignore_errors=True)


async def _run() -> int:  # noqa: PLR0911, PLR0915 - a linear end-to-end script
    if not require_live_llm_tests("live harness-profile integration"):
        return 0
    if shutil.which("agy") is None:
        print("SKIP — `agy` CLI not on PATH")
        return 0

    print(f"BETTER_AGENT_HOME = {BA_HOME}")
    _test_installation.activate(Path(BA_HOME))

    import harness_profile_resolver  # noqa: PLC0415
    import harness_run_projection  # noqa: PLC0415
    from session_manager import manager as session_manager  # noqa: PLC0415

    port = harness.free_port()
    server = harness.BackgroundUvicorn("main:app", port)
    server.start()
    cwd = tempfile.mkdtemp(prefix="bc-int-harness-profile-cwd-")
    cookie = harness.mint_session_cookie()
    failures = 0

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=300,
            cookies={"better_agent_session": cookie},
        ) as client:
            response = await client.get("/api/auth/me")
            if response.status_code != 200:
                _fail("auth probe", f"HTTP {response.status_code}: {response.text[:200]}")
                return 1
            _ok("auth cookie accepted")

            live = LiveHarness(client, f"ws://127.0.0.1:{port}/ws/chat", cookie, cwd)
            await live.use_cheap_provider()
            _ok(f"agy provider active on {CHEAP_MODEL!r}")

            # Until the builtin extensions finish installing, Default resolves
            # a projection that governs nothing and a turn runs unscoped. The
            # comparison below is only meaningful once the harness is real, so
            # require that precondition instead of racing it.
            governed = await _await_governed_default(
                harness_profile_resolver, harness_run_projection,
            )
            if not governed:
                _fail("harness readiness", "Default never governed any extension")
                return 1
            _ok(f"Default profile governs {len(governed)} extensions")

            # --- Control: Default profile, real turn, real tool surface. ---
            control_sid = await live.session_on("Control")
            control_text, reason = await _speak(live, control_sid)
            if reason:
                if harness.provider_unavailable(reason):
                    print(f"SKIP — the provider is unavailable: {reason}")
                    return 0
                _fail("control turn", reason)
                return 1
            _ok(f"control session answered: {control_text[:40]!r}")

            control_run = _run_dir_for(control_sid)
            if control_run is None:
                _fail("control run dir", "no run carried this session id")
                return 1
            control_servers = _cli_mcp_servers(control_run)
            if not control_servers:
                _fail("control tool surface", f"no mcpServers in {control_run / CLI_SETTINGS}")
                return 1
            _ok(f"control run loaded {len(control_servers)} MCP servers")

            # The CLI must confirm it read THIS run's overlay — otherwise the
            # settings file proves nothing about what the model was given.
            app_data = _cli_app_data_dir(control_run)
            expected = str((control_run / CLI_SETTINGS).parent)
            if os.path.realpath(app_data) != os.path.realpath(expected):
                _fail("cli config dir", f"CLI used {app_data!r}, run overlay is {expected!r}")
                failures += 1
            else:
                _ok("agy CLI confirms it read the run's overlay config")

            # --- The profile switches off one server the control run had. ---
            descriptor = (await client.get("/api/harness-profiles/descriptor")).json()
            pair = _first_extension_mcp_pair(descriptor, control_servers)
            if pair is None:
                _fail("target server", f"no descriptor MCP item among {sorted(control_servers)}")
                return 1
            extension_id, server_name = pair

            profile = await live.profile_disabling("Live Profile", [
                {"path": ["extension_instances", extension_id, "mcp_servers", server_name],
                 "value": False},
                {"path": ["disabled_builtin_tools", DISABLED_TOOL], "value": False},
            ])
            _ok(f"profile turns off MCP server {server_name!r} of {extension_id!r}")

            # The write must stay scoped to the profile, or the control run's
            # surface would have been the same by accident.
            response = await client.get("/api/harness/default/disabled-builtin-tools")
            if DISABLED_TOOL in (response.json().get("disabled_builtin_tools") or []):
                _fail("profile isolation", "the named-profile write leaked into Default")
                failures += 1
            else:
                _ok("Default is untouched by the profile write")

            # --- Scoped: same provider, same model, same prompt. ---
            scoped_sid = await live.session_on("Scoped", profile)
            stored = session_manager.get(scoped_sid) or {}
            if stored.get("harness_profile_id") != profile["id"]:
                _fail("session binding", f"persisted profile={stored.get('harness_profile_id')!r}")
                return 1

            snapshot = harness_profile_resolver.resolve_for_session(stored)
            if not harness_run_projection.is_active(snapshot):
                _fail("run snapshot", f"inactive snapshot: {snapshot}")
                return 1
            _ok(f"session {scoped_sid[:8]} bound to the profile")

            scoped_text, reason = await _speak(live, scoped_sid)
            if reason:
                if harness.provider_unavailable(reason):
                    print(f"SKIP — the provider is unavailable: {reason}")
                    return 0
                _fail("scoped turn", reason)
                return 1
            _ok(f"scoped session answered: {scoped_text[:40]!r}")

            scoped_run = _run_dir_for(scoped_sid)
            if scoped_run is None:
                _fail("scoped run dir", "no run carried this session id")
                return 1

            # 1) The override crossed the process boundary into the runner.
            spawned = json.loads((scoped_run / "input.json").read_text(encoding="utf-8"))
            if (spawned.get("resolved_harness_run_config") or {}).get("profile_id") != profile["id"]:
                _fail("spawn payload", f"profile_id={spawned.get('resolved_harness_run_config')}")
                failures += 1
            elif DISABLED_TOOL not in (spawned.get("disabled_builtin_tools") or []):
                _fail("spawn payload", f"disabled_builtin_tools={spawned.get('disabled_builtin_tools')}")
                failures += 1
            else:
                _ok("spawned runner received the profile id and its overrides")

            # 2) The tool surface the CLI loaded lost exactly that server.
            scoped_servers = _cli_mcp_servers(scoped_run)
            if server_name in scoped_servers:
                _fail("tool surface", f"{server_name!r} still offered to the model")
                failures += 1
            elif control_servers - scoped_servers != {server_name}:
                _fail(
                    "tool surface",
                    f"expected only {server_name!r} to drop, lost {sorted(control_servers - scoped_servers)}",
                )
                failures += 1
            else:
                _ok(f"model's MCP surface lost exactly {server_name!r} ({len(scoped_servers)} left)")

            app_data = _cli_app_data_dir(scoped_run)
            expected = str((scoped_run / CLI_SETTINGS).parent)
            if os.path.realpath(app_data) != os.path.realpath(expected):
                _fail("cli config dir", f"CLI used {app_data!r}, run overlay is {expected!r}")
                failures += 1
            else:
                _ok("agy CLI confirms the scoped turn ran on the scoped overlay")

            # 3) The binding survives a completed turn.
            after = session_manager.get(scoped_sid) or {}
            if after.get("harness_profile_id") != profile["id"]:
                _fail("binding after turn", f"profile={after.get('harness_profile_id')!r}")
                failures += 1
            else:
                _ok("profile binding intact after the turn")
    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)

    print()
    if failures:
        print(f"\033[91m{failures} FAILURE(S)\033[0m")
        return 1
    print("\033[92mALL PASS — the profile reaches the model's real tool surface\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

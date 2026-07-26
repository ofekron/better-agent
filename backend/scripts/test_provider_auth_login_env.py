"""Locks the per-account OAuth login/logout spawn for subscription providers.

The settings-UI login spawns the provider's own CLI (`claude auth login` /
`codex login`) with the record's isolated credential env, so two records
with distinct config_dirs log in as distinct accounts. This test locks:

- `supports_auth` is true only for subscription claude/codex records.
- The spawn argv is the fixed kind->suffix mapping (no caller input), and
  the subprocess env carries the record's `CLAUDE_CONFIG_DIR` / `CODEX_HOME`
  override while clearing cross-provider credential env.
- `detach_login_state` surfaces auth-flow state to the UI.

The real OAuth browser flow is NOT exercised: the CLI binary resolution and
subprocess spawn are stubbed so the test captures argv+env without opening
a browser or touching the network.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="provider_auth_home_")
paths.engage_test_home(_TMP)

import config_store  # noqa: E402
import provider_auth  # noqa: E402

HOME = Path(_TMP)


def _add_provider(kind: str, config_dir: str, mode: str = "subscription") -> dict:
    return config_store.add_provider(
        {
            "name": f"{kind}-{config_dir}",
            "kind": kind,
            "mode": mode,
            "config_dir": config_dir,
        }
    )


def test_supports_auth_only_subscription_claude_codex():
    sub_claude = _add_provider("claude", str(HOME / ".claude-a"))
    sub_codex = _add_provider("codex", str(HOME / ".codex-a"))
    api_key_claude = _add_provider("claude", str(HOME / ".claude-b"), mode="api_key")
    assert provider_auth.supports_auth(sub_claude)
    assert provider_auth.supports_auth(sub_codex)
    assert not provider_auth.supports_auth(api_key_claude)


def test_build_env_isolates_credential_dir():
    claude = _add_provider("claude", str(HOME / ".claude-work"))
    codex = _add_provider("codex", str(HOME / ".codex-work"))
    os.environ["ANTHROPIC_API_KEY"] = "leaked-key"
    os.environ["CODEX_HOME"] = "/tmp/should-not-leak"
    try:
        c_env = provider_auth._build_env(claude)
        assert c_env["CLAUDE_CONFIG_DIR"] == str(HOME / ".claude-work")
        assert "ANTHROPIC_API_KEY" not in c_env
        # codex's own override must not leak into a claude login.
        assert "CODEX_HOME" not in c_env

        x_env = provider_auth._build_env(codex)
        assert x_env["CODEX_HOME"] == str(HOME / ".codex-work")
        assert "CLAUDE_CONFIG_DIR" not in x_env
        # ambient CODEX_HOME is cleared, then the record's dir overrides
        assert x_env["CODEX_HOME"] != "/tmp/should-not-leak"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("CODEX_HOME", None)


def test_start_login_spawns_fixed_argv_with_isolated_env():
    claude = _add_provider("claude", str(HOME / ".claude-login"))
    spawns: list[dict] = []

    async def fake_create_subprocess_exec(*argv, **kwargs):
        spawns.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

            def kill(self):
                pass

        return _FakeProc()

    def fake_resolve_binary(_kind):
        return "/usr/local/bin/claude"

    # The monitor's authoritative status check would spawn again; stub the
    # binary resolver so it reports the same fake binary, and stub status
    # to a quick no-op subprocess returning 0 via the same fake spawner.
    original_create = asyncio.create_subprocess_exec
    original_resolve = provider_auth._resolve_binary
    asyncio.create_subprocess_exec = fake_create_subprocess_exec  # type: ignore
    provider_auth._resolve_binary = fake_resolve_binary  # type: ignore
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _drive():
            result = await provider_auth.start_login(
                claude["id"], broadcast=lambda: asyncio.sleep(0)
            )
            # let the monitor task finish
            await asyncio.sleep(0)
            return result

        result = loop.run_until_complete(_drive())
    finally:
        asyncio.create_subprocess_exec = original_create  # type: ignore
        provider_auth._resolve_binary = original_resolve  # type: ignore

    assert result["ok"], result
    # First spawn is the login command; the monitor's status check may
    # spawn again afterwards.
    login_spawn = spawns[0]
    assert login_spawn["argv"][:3] == ["/usr/local/bin/claude", "auth", "login"]
    assert login_spawn["env"]["CLAUDE_CONFIG_DIR"] == str(HOME / ".claude-login")


def test_detach_login_state_adds_state_for_supported_records():
    claude = _add_provider("claude", str(HOME / ".claude-detach"))
    detached = provider_auth.detach_login_state(dict(claude))
    assert "login_state" in detached
    assert detached["login_state"]["status"] == provider_auth.STATE_IDLE


def test_concurrent_login_is_rejected():
    claude = _add_provider("claude", str(HOME / ".claude-concurrent"))

    # Simulate an in-flight login by marking the record's state running.
    provider_auth._set_state(claude["id"], provider_auth.STATE_LOGIN_RUNNING)

    async def _drive():
        return await provider_auth.start_login(
            claude["id"], broadcast=lambda: asyncio.sleep(0)
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(_drive())
    assert result["error"] == "busy"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"  {name} ...", end=" ")
            fn()
            print("ok")
    print("all provider_auth tests passed")

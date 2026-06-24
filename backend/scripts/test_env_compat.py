"""Regression test for Better Agent env-var compatibility.

Run with:
    cd backend && python scripts/test_env_compat.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

backend = Path(__file__).resolve().parents[1]
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))

from env_compat import dual_env, get_env, require_env
from provider import build_better_agent_run_env


def _clear(*names: str) -> None:
    for name in names:
        os.environ.pop(name, None)


def main() -> int:
    _clear("BETTER_AGENT_BACKEND_URL", "BETTER_CLAUDE_BACKEND_URL")
    os.environ["BETTER_CLAUDE_BACKEND_URL"] = "http://legacy"
    assert get_env("BETTER_CLAUDE_BACKEND_URL") == "http://legacy"
    os.environ["BETTER_AGENT_BACKEND_URL"] = "http://agent"
    assert get_env("BETTER_CLAUDE_BACKEND_URL") == "http://agent"
    assert require_env("BETTER_CLAUDE_BACKEND_URL") == "http://agent"

    pair = dual_env("BETTER_CLAUDE_MODEL", "sonnet")
    assert pair == {
        "BETTER_AGENT_MODEL": "sonnet",
        "BETTER_CLAUDE_MODEL": "sonnet",
    }

    run_env = build_better_agent_run_env(
        backend_url="http://backend",
        internal_token="token",
        app_session_id="sid",
        cwd="/repo",
        model="model",
        provider_id="provider",
        bare_config=False,
        user_facing=True,
        disabled_builtin_extensions=["b", "a"],
    )
    for suffix in (
        "BACKEND_URL",
        "INTERNAL_TOKEN",
        "APP_SESSION_ID",
        "CWD",
        "MODEL",
        "PROVIDER_ID",
        "BARE_CONFIG",
        "USER_FACING",
        "DISABLED_BUILTIN_EXTENSIONS",
    ):
        assert run_env[f"BETTER_AGENT_{suffix}"] == run_env[f"BETTER_CLAUDE_{suffix}"]
    assert run_env["BETTER_AGENT_DISABLED_BUILTIN_EXTENSIONS"] == "a,b"
    print("PASS env compatibility")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

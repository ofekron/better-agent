#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

IMPORT_PROBE = r"""
import importlib
import json
import os
import sys

import config_store

config_store._save_state(config_store._seed_default_state())
state = json.loads(config_store._config_path().read_text(encoding="utf-8"))
state["schema_version"] = config_store.MIN_SUPPORTED_CONFIG_SCHEMA_VERSION
state.pop("provider_state_authority")
state.pop("provider_state_projected")
state.pop("runtime_profiles")
state.pop("default_runtime_profile_id")
state.pop("deleted_providers")
active = next(
    provider
    for provider in state["providers"]
    if provider["id"] == state["default_provider_id"]
)
active["config_dir"] = os.environ["PROVIDER_CONFIG_PROBE"]
provider_authority = {
    provider["id"]: (provider["generation"], provider["revision"])
    for provider in state["providers"]
}
config_store._config_path().parent.mkdir(parents=True, exist_ok=True)
config_store._config_path().write_text(json.dumps(state), encoding="utf-8")
with config_store._state_cache_lock:
    config_store._state_cache = None

importlib.import_module(sys.argv[1])

persisted = json.loads(config_store._config_path().read_text(encoding="utf-8"))
assert persisted["schema_version"] == config_store.CONFIG_SCHEMA_VERSION
assert {
    provider["id"]: (provider["generation"], provider["revision"])
    for provider in persisted["providers"]
} == provider_authority
assert persisted["provider_state_projected"] is False
assert persisted["provider_state_authority"]["digest"] == (
    config_store.provider_sync_authority.snapshot_digest(
        persisted["default_provider_id"],
        persisted["providers"],
    )
)
persisted_active = next(
    provider
    for provider in persisted["providers"]
    if provider["id"] == persisted["default_provider_id"]
)
env_var, expected = config_store.provider_credential_env(persisted_active)
assert os.environ[env_var] == expected
"""


def main() -> None:
    for entrypoint in ("main", "main_node"):
        with tempfile.TemporaryDirectory(
            prefix=f"better-agent-{entrypoint}-config-startup-"
        ) as home:
            env = os.environ.copy()
            env.update({
                "BETTER_AGENT_HOME": home,
                "BETTER_AGENT_TEST_MODE": "1",
                "BETTER_CLAUDE_API_ONLY": "1",
                "PROVIDER_CONFIG_PROBE": str(Path(home) / "provider-config"),
            })
            env.pop("CLAUDE_CONFIG_DIR", None)
            env.pop("ANTHROPIC_BASE_URL", None)
            subprocess.run(
                [sys.executable, "-c", IMPORT_PROBE, entrypoint],
                cwd=BACKEND,
                env=env,
                check=True,
                timeout=60,
            )
    print("PASS provider config startup loading")


if __name__ == "__main__":
    main()

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_HOME = _test_home.isolate("bc-test-cli-provider-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import config_store


def main() -> int:
    try:
        z = config_store.add_provider({
            "name": "Z.AI",
            "kind": "claude",
            "mode": "api_key",
            "base_url": "https://api.z.ai/api/anthropic",
            "config_dir": "$HOME/.claude-zai",
            "default_model": "glm-5.1",
        })
        z_default = config_store.add_provider({
            "name": "Z.AI default",
            "kind": "claude",
            "mode": "api_key",
            "base_url": "https://api.z.ai/api/anthropic",
            "default_model": "glm-5.1",
        })
        z_braced_home = config_store.add_provider({
            "name": "Z.AI braced home",
            "kind": "claude",
            "mode": "api_key",
            "base_url": "https://api.z.ai/api/anthropic",
            "config_dir": "${HOME}/.claude-zai",
            "default_model": "glm-5.1",
        })
        z_custom = config_store.add_provider({
            "name": "Z.AI custom",
            "kind": "claude",
            "mode": "api_key",
            "base_url": "https://api.z.ai/api/anthropic",
            "config_dir": "~/custom-zai",
            "default_model": "glm-5.1",
        })
        active_before = config_store.list_providers()["default_provider_id"]
        assert z["config_dir"] == "~/.claude-zai"
        assert z_default["config_dir"] == "~/.claude-zai"
        assert z_braced_home["config_dir"] == "~/.claude-zai"
        assert z_custom["config_dir"] == "~/custom-zai"

        assert cli.resolve_provider("Z.AI")["id"] == z["id"]
        assert cli.resolve_provider(z["id"])["id"] == z["id"]

        config_store.apply_env_vars(z["id"])
        assert os.environ["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert os.environ["CLAUDE_CONFIG_DIR"].endswith("/.claude-zai")
        assert not os.environ["CLAUDE_CONFIG_DIR"].startswith(("~", "$HOME"))
        assert config_store.list_providers()["default_provider_id"] == active_before

        print("PASS: CLI provider selection is explicit and does not change active provider")
        return 0
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

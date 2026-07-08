from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_HOME = _test_home.isolate("bc-test-ollama-provider-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_store
from paths import user_home
from provider_claude import ClaudeProvider


def main() -> int:
    original_read_api_key = config_store._read_api_key
    original_write_api_key = config_store._write_api_key
    keys: dict[str, str] = {}
    try:
        os.environ["CLAUDE_CODE_SIMPLE"] = "1"
        config_store._read_api_key = lambda provider_id: keys.get(provider_id, "")
        config_store._write_api_key = lambda provider_id, value: keys.__setitem__(provider_id, value)

        provider = config_store.add_provider({
            "name": "Ollama",
            "kind": "claude",
            "mode": "api_key",
            "api_key": "ollama",
            "base_url": "http://localhost:11434",
            "config_dir": "$HOME/.claude-ollama",
            "default_model": "qwen3-coder",
            "default_reasoning_effort": "medium",
        })

        config_store.apply_env_vars(provider["id"])
        assert os.environ["ANTHROPIC_API_KEY"] == "ollama"
        assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "ollama"
        assert os.environ["ANTHROPIC_BASE_URL"] == "http://localhost:11434"

        record = config_store.get_provider_with_key(provider["id"])
        assert record is not None
        env = ClaudeProvider(record).build_env()
        assert env["ANTHROPIC_API_KEY"] == "ollama"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
        assert env["CLAUDE_CONFIG_DIR"].endswith("/.claude-ollama")
        assert env["HOME"] == str(user_home())
        assert "CLAUDE_CODE_SIMPLE" not in env

        remote = config_store.add_provider({
            "name": "Remote Anthropic-compatible",
            "kind": "claude",
            "mode": "api_key",
            "api_key": "remote-key",
            "base_url": "https://api.example.com",
            "default_model": "remote-model",
        })
        remote_record = config_store.get_provider_with_key(remote["id"])
        assert remote_record is not None
        remote_env = ClaudeProvider(remote_record).build_env()
        assert remote_env["ANTHROPIC_API_KEY"] == "remote-key"
        assert "ANTHROPIC_AUTH_TOKEN" not in remote_env

        subscription_env = ClaudeProvider({
            "id": "subscription",
            "kind": "claude",
            "mode": "subscription",
            "config_dir": "",
        }).build_env()
        assert subscription_env["HOME"] == str(user_home())
        assert "CLAUDE_CONFIG_DIR" not in subscription_env

        default_config_env = ClaudeProvider({
            "id": "subscription-default-config",
            "kind": "claude",
            "mode": "subscription",
            "config_dir": "~/.claude",
        }).build_env()
        assert default_config_env["HOME"] == str(user_home())
        assert "CLAUDE_CONFIG_DIR" not in default_config_env

        home_default_config_env = ClaudeProvider({
            "id": "subscription-home-default-config",
            "kind": "claude",
            "mode": "subscription",
            "config_dir": "$HOME/.claude",
        }).build_env()
        assert home_default_config_env["HOME"] == str(user_home())
        assert "CLAUDE_CONFIG_DIR" not in home_default_config_env

        print("PASS: Ollama Claude provider exports local auth token only for local endpoints")
        return 0
    finally:
        config_store._read_api_key = original_read_api_key
        config_store._write_api_key = original_write_api_key
        shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

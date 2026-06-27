"""openai (BA-owned agent loop) has no subscription auth — only api_key.

Locks the backend enforcement in config_store: add_provider/update_provider
must reject kind=openai + mode=subscription and accept kind=openai + api_key.
Mirrors the existing gemini-subscription rejection.

Uses a temp BETTER_AGENT_HOME so no real session state is touched.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="openai_rej_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import config_store  # noqa: E402


def test_add_openai_subscription_rejected():
    try:
        config_store.add_provider(
            {"name": "Bad", "kind": "openai", "mode": "subscription"}
        )
    except ValueError as e:
        assert "subscription" in str(e).lower(), e
        return
    raise AssertionError("openai+subscription should have been rejected")


def test_add_openai_api_key_accepted():
    p = config_store.add_provider(
        {
            "name": "Sakana",
            "kind": "openai",
            "mode": "api_key",
            "base_url": "https://api.sakana.ai/v1",
            "default_model": "fugu",
        }
    )
    assert p["kind"] == "openai" and p["mode"] == "api_key", p


def test_update_to_openai_subscription_rejected():
    p = config_store.add_provider(
        {"name": "Flip", "kind": "openai", "mode": "api_key"}
    )
    try:
        config_store.update_provider(p["id"], {"mode": "subscription"})
    except ValueError as e:
        assert "subscription" in str(e).lower(), e
        return
    raise AssertionError("update to openai+subscription should have been rejected")


if __name__ == "__main__":
    test_add_openai_subscription_rejected()
    test_add_openai_api_key_accepted()
    test_update_to_openai_subscription_rejected()
    print("ok")

import sys
from datetime import datetime, timezone
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-provider-rate-limit-parsing-")
_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from provider_claude import ClaudeProvider  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from provider_gemini import GeminiProvider  # noqa: E402


def test_generic_rate_limits_use_scheduler_fallback() -> None:
    providers = [
        ClaudeProvider({"id": "claude", "name": "Claude", "kind": "claude"}),
        CodexProvider({"id": "codex", "name": "Codex", "kind": "codex"}),
        GeminiProvider({"id": "gemini", "name": "Gemini", "kind": "gemini"}),
    ]
    for provider in providers:
        assert provider.parse_rate_limit("HTTP 429: too many requests", []) is None


def test_specific_provider_resets_still_parse() -> None:
    claude = ClaudeProvider({"id": "claude", "name": "Claude", "kind": "claude"})
    reset = claude.parse_rate_limit("Rate limit reached, resets 11pm", [])
    assert reset is not None
    assert reset > datetime.now(timezone.utc)

    gemini = GeminiProvider({"id": "gemini", "name": "Gemini", "kind": "gemini"})
    reset = gemini.parse_rate_limit("RESOURCE_EXHAUSTED daily quota", [])
    assert reset is not None
    assert reset > datetime.now(timezone.utc)


if __name__ == "__main__":
    test_generic_rate_limits_use_scheduler_fallback()
    test_specific_provider_resets_still_parse()
    print("ALL PASS")

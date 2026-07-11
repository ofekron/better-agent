import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import _test_home

_test_home.isolate("bc-test-provider-rate-limit-parsing-")
_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from provider_claude import ClaudeProvider  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from provider_gemini import GeminiProvider  # noqa: E402
from rate_limits import is_rate_limit_text, parse_rate_limit  # noqa: E402
from turn_helpers import _is_rate_limit_attempt  # noqa: E402


def test_generic_rate_limits_schedule_one_hour_reset() -> None:
    providers = [
        ClaudeProvider({"id": "claude", "name": "Claude", "kind": "claude"}),
        CodexProvider({"id": "codex", "name": "Codex", "kind": "codex"}),
        GeminiProvider({"id": "gemini", "name": "Gemini", "kind": "gemini"}),
    ]
    for provider in providers:
        before = datetime.now(timezone.utc)
        reset = provider.parse_rate_limit("HTTP 429: too many requests", [])
        assert reset is not None
        assert timedelta(minutes=59) <= reset - before <= timedelta(hours=1, seconds=2)


def test_specific_provider_resets_still_parse() -> None:
    claude = ClaudeProvider({"id": "claude", "name": "Claude", "kind": "claude"})
    reset = claude.parse_rate_limit("Rate limit reached, resets 11pm", [])
    assert reset is not None
    assert reset > datetime.now(timezone.utc)


def test_claude_rollovers_are_deterministic() -> None:
    now = datetime(2026, 12, 12, 22, 0, tzinfo=timezone.utc)
    reset = parse_rate_limit(
        "claude", "Rate limit reached, resets Dec 11 at 11pm", now=now,
    )
    assert reset == datetime(2027, 12, 11, 23, 0, tzinfo=timezone.utc)

    now = datetime(2026, 7, 11, 22, 0, tzinfo=timezone.utc)
    reset = parse_rate_limit("claude", "Limit reached, resets 9pm", now=now)
    assert reset == datetime(2026, 7, 12, 21, 0, tzinfo=timezone.utc)


def test_codex_try_again_timestamp_and_ordinal() -> None:
    now = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    reset = parse_rate_limit(
        "codex",
        "Usage limit reached. Try again at July 11th, 2026 9:30 PM.",
        now=now,
    )
    assert reset == datetime(2026, 7, 11, 21, 30, tzinfo=timezone.utc)


def test_wall_clock_resets_keep_machine_timezone() -> None:
    local_tz = ZoneInfo("Asia/Jerusalem")
    now = datetime(2026, 7, 11, 20, 0, tzinfo=local_tz)
    reset = parse_rate_limit("claude", "Rate limit reached, resets 9pm", now=now)
    assert reset == datetime(2026, 7, 11, 21, 0, tzinfo=local_tz)


def test_gemini_reset_after_duration() -> None:
    now = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)
    reset = parse_rate_limit(
        "gemini",
        "RESOURCE_EXHAUSTED: quota reset after 18h48m27s",
        now=now,
    )
    assert reset == now + timedelta(hours=18, minutes=48, seconds=27)


def test_event_text_and_tail_detection() -> None:
    raw_events = [{
        "type": "agent_message",
        "data": {"type": "assistant", "message": {"content": [{
            "type": "text", "text": "No more messages until the limit resets",
        }]}},
    }]
    claude = ClaudeProvider({"id": "claude", "name": "Claude", "kind": "claude"})
    assert not _is_rate_limit_attempt(None, raw_events)
    assert claude.parse_rate_limit(None, raw_events) is not None
    explicit_error_events = [{
        "type": "agent_message",
        "data": {"type": "assistant", "message": {"content": [{
            "type": "text", "text": "API Error: 429 too many requests",
        }]}},
    }]
    assert _is_rate_limit_attempt(None, explicit_error_events)
    assert is_rate_limit_text("x" * 3000 + " quota exceeded")
    assert not is_rate_limit_text(429)
    assert not is_rate_limit_text("ordinary provider failure")

    gemini = GeminiProvider({"id": "gemini", "name": "Gemini", "kind": "gemini"})
    reset = gemini.parse_rate_limit("RESOURCE_EXHAUSTED daily quota", [])
    assert reset is not None
    assert reset > datetime.now(timezone.utc)


if __name__ == "__main__":
    test_generic_rate_limits_schedule_one_hour_reset()
    test_specific_provider_resets_still_parse()
    test_claude_rollovers_are_deterministic()
    test_codex_try_again_timestamp_and_ordinal()
    test_wall_clock_resets_keep_machine_timezone()
    test_gemini_reset_after_duration()
    test_event_text_and_tail_detection()
    print("ALL PASS")

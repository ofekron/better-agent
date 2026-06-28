"""Regression: OpenAIProvider.parse_rate_limit must classify 429s so the
orchestrator's rate-limit retry loop (turn_manager) works.

Pre-fix, OpenAIProvider had NO parse_rate_limit. On a 429, turn_manager's
`provider.parse_rate_limit(...)` call raised AttributeError (unwrapped),
aborting the turn instead of retrying — 7 fugu runs died this way with
"Subscription window is exceeded".

Post-fix: subscription-window / quota exhaustion → long reset (+1h);
per-minute throttle → short reset (+1min); unrelated text → None (loop falls
through, no retry).
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import provider_openai  # noqa: E402


def _provider() -> provider_openai.OpenAIProvider:
    return provider_openai.OpenAIProvider(
        {"id": "t", "name": "t", "kind": "openai", "mode": "api_key", "base_url": "x"}
    )


def test_subscription_window_429_is_long_reset():
    p = _provider()
    err = 'RuntimeError: HTTP 429: {"error":{"message":"Subscription window is exceeded"}}'
    reset = p.parse_rate_limit(err, [])
    assert reset is not None
    delta = (reset - datetime.now(timezone.utc)).total_seconds()
    # ~1h (orchestrator clamps the actual wait to 600s; this is the honest
    # reset time surfaced to the UI as retrying_until).
    assert 3000 <= delta <= 3700, delta


def test_per_minute_429_is_short_reset():
    p = _provider()
    reset = p.parse_rate_limit("HTTP 429: too many requests", [])
    assert reset is not None
    delta = (reset - datetime.now(timezone.utc)).total_seconds()
    assert 30 <= delta <= 90, delta


def test_unrelated_error_is_none():
    p = _provider()
    assert p.parse_rate_limit("HTTP 500: internal server error", []) is None
    assert p.parse_rate_limit("", []) is None


def test_long_keywords_win_over_short_when_both_present():
    p = _provider()
    # quota exceeded (long) + 429 (matches both) → long.
    reset = p.parse_rate_limit("HTTP 429: quota exceeded", [])
    delta = (reset - datetime.now(timezone.utc)).total_seconds()
    assert delta > 3000, delta


if __name__ == "__main__":
    sys.exit(0)

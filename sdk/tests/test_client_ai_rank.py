"""Unit test for Client.ai_rank's request shape.

Run with:
    cd sdk && python3 tests/test_client_ai_rank.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK = os.path.dirname(_HERE)
if _SDK not in sys.path:
    sys.path.insert(0, _SDK)

import better_agent_sdk  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _client_with_captured_post():
    client = better_agent_sdk.Client(
        backend_url="http://localhost:1", internal_token="t", app_session_id="default-sid",
    )
    calls: list[tuple[str, dict, float]] = []
    client._post = lambda path, payload, **kw: (
        calls.append((path, payload, kw.get("timeout"))),
        {"ids": [], "reasoning": "", "error": None},
    )[1]
    return client, calls


def _ai_rank_posts_expected_path_and_body() -> bool:
    client, calls = _client_with_captured_post()
    out = client.ai_rank("cards", "login bug", [{"id": "a"}], 5)
    ok = (
        len(calls) == 1
        and calls[0][0] == "/api/internal/ai-rank"
        and calls[0][1] == {
            "kind": "cards",
            "query": "login bug",
            "candidates": [{"id": "a"}],
            "max_results": 5,
        }
        and out == {"ids": [], "reasoning": "", "error": None}
    )
    print(f"{OK if ok else FAIL} ai_rank posts kind/query/candidates/max_results (got {calls}, {out})")
    return ok


def _ai_rank_forwards_explicit_timeout() -> bool:
    client, calls = _client_with_captured_post()
    client.ai_rank("cards", "q", [], 1, timeout=5.0)
    ok = calls[0][2] == 5.0
    print(f"{OK if ok else FAIL} ai_rank forwards an explicit timeout (got {calls[0][2]})")
    return ok


def _ai_rank_defaults_timeout_when_omitted() -> bool:
    client, calls = _client_with_captured_post()
    client.ai_rank("cards", "q", [], 1)
    ok = calls[0][2] is not None
    print(f"{OK if ok else FAIL} ai_rank defaults timeout when omitted (got {calls[0][2]})")
    return ok


_CHECKS = (
    _ai_rank_posts_expected_path_and_body,
    _ai_rank_forwards_explicit_timeout,
    _ai_rank_defaults_timeout_when_omitted,
)


def test_ai_rank_contract() -> None:
    assert all(check() for check in _CHECKS)


def main_run() -> int:
    results = [check() for check in _CHECKS]
    n_pass = sum(1 for r in results if r)
    print(f"\n{n_pass}/{len(_CHECKS)} ai_rank tests passed")
    return 0 if n_pass == len(_CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main_run())

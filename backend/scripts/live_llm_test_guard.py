from __future__ import annotations

import os

LIVE_LLM_TESTS_ENV = "RUN_LLM_TESTS"


def live_llm_tests_enabled() -> bool:
    return os.environ.get(LIVE_LLM_TESTS_ENV) == "1"


def live_llm_skip_message(test_name: str) -> str:
    return f"{test_name} requires {LIVE_LLM_TESTS_ENV}=1"


def require_live_llm_tests(test_name: str) -> bool:
    if live_llm_tests_enabled():
        return True
    print(f"SKIP - {live_llm_skip_message(test_name)}")
    return False

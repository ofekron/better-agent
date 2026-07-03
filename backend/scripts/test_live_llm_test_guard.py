from __future__ import annotations

import live_llm_test_guard


def test_live_llm_tests_disabled_by_default(monkeypatch):
    monkeypatch.delenv(live_llm_test_guard.LIVE_LLM_TESTS_ENV, raising=False)

    assert live_llm_test_guard.live_llm_tests_enabled() is False


def test_live_llm_tests_require_exact_opt_in(monkeypatch):
    for value in ("true", "yes", "0", " 1"):
        monkeypatch.setenv(live_llm_test_guard.LIVE_LLM_TESTS_ENV, value)

        assert live_llm_test_guard.live_llm_tests_enabled() is False

    monkeypatch.setenv(live_llm_test_guard.LIVE_LLM_TESTS_ENV, "1")

    assert live_llm_test_guard.live_llm_tests_enabled() is True


def test_require_live_llm_tests_skips_without_opt_in(monkeypatch, capsys):
    monkeypatch.delenv(live_llm_test_guard.LIVE_LLM_TESTS_ENV, raising=False)

    assert live_llm_test_guard.require_live_llm_tests("live provider test") is False
    assert "live provider test requires RUN_LLM_TESTS=1" in capsys.readouterr().out


def test_require_live_llm_tests_allows_exact_opt_in(monkeypatch, capsys):
    monkeypatch.setenv(live_llm_test_guard.LIVE_LLM_TESTS_ENV, "1")

    assert live_llm_test_guard.require_live_llm_tests("live provider test") is True
    assert capsys.readouterr().out == ""

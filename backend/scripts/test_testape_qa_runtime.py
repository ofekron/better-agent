from __future__ import annotations

import _test_home

_test_home.isolate("bc-test-testape-qa-runtime-")

import pytest
import testape_qa_runtime


def test_capability_claims_persist_and_reject_foreign_sessions(monkeypatch) -> None:
    monkeypatch.setenv("BETTER_AGENT_TESTAPE_QA_TOKEN", "qa-secret")
    monkeypatch.setenv("BETTER_AGENT_TESTAPE_QA_RUNTIME_ID", "runtime-a")

    assert testape_qa_runtime.authorize("qa-secret") == "runtime-a"
    with pytest.raises(PermissionError, match="invalid TestApe QA capability"):
        testape_qa_runtime.authorize("wrong")

    testape_qa_runtime.claim("runtime-a", "controller")
    testape_qa_runtime.claim("runtime-a", "case")
    testape_qa_runtime.assert_owned("runtime-a", "controller", "case")
    with pytest.raises(PermissionError, match="not owned"):
        testape_qa_runtime.assert_owned("runtime-a", "controller", "foreign")
    with pytest.raises(PermissionError, match="ownership mismatch"):
        testape_qa_runtime.claim("runtime-b", "other")

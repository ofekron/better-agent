#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path


TEST_HOME = tempfile.mkdtemp(prefix="ba-headless-contract-")
os.environ["BETTER_AGENT_HOME"] = TEST_HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headless_request_contract import (  # noqa: E402
    AdmittedHeadlessRequest,
    HeadlessAdmissionError,
    HeadlessAuthority,
    HeadlessRequest,
    admit_headless_request,
)


def _provider(kind: str) -> dict:
    return {
        "id": f"{kind}-provider",
        "kind": kind,
        "generation": str(uuid.uuid4()),
        "revision": 7,
    }


def _authority(kind: str = "claude", **overrides) -> HeadlessAuthority:
    values = {
        "owner_kind": "session",
        "owner_id": "session-1",
        "provider": _provider(kind),
        "model": "model-1",
        "reasoning_effort": "high",
        "runner": "subscription",
        "permission_scope": "spawn_runs",
        "routing": {"node_id": "primary"},
        "cwd": "/tmp/project",
        "resume_sid": "provider-session-1",
        "supports_fork": True,
        "supports_no_tools": True,
    }
    values.update(overrides)
    return HeadlessAuthority.create(**values)


def _request(**overrides) -> HeadlessRequest:
    raw = {
        "schema": 1,
        "prompt": "answer precisely",
        "owner": {"kind": "session", "id": "session-1"},
        "fork": True,
        "no_tools": True,
        "timeout": 60.0,
    }
    raw.update(overrides)
    return HeadlessRequest.from_dict(raw)


def _raises(fn, contains: str) -> None:
    try:
        fn()
    except (ValueError, HeadlessAdmissionError) as exc:
        assert contains in str(exc), (contains, str(exc))
        return
    raise AssertionError(f"expected failure containing {contains!r}")


def test_request_schema_is_exact_and_non_coercive() -> None:
    _raises(lambda: _request(fork="true"), "fork")
    _raises(lambda: _request(no_tools=1), "no_tools")
    _raises(lambda: _request(timeout=True), "timeout")
    _raises(lambda: _request(api_key="secret"), "invalid headless request")
    _raises(lambda: _request(executable="/tmp/evil"), "invalid headless request")
    _raises(lambda: _request(config={"env": {}}), "invalid headless request")
    _raises(lambda: _request(permission="spawn_runs"), "invalid headless request")
    _raises(
        lambda: _request(owner={"kind": "session", "id": ""}),
        "owner",
    )
    _raises(
        lambda: _request(owner={"kind": "standalone", "id": "session-1"}),
        "owner",
    )
    _raises(
        lambda: admit_headless_request(
            HeadlessRequest(
                prompt="bypass",
                owner_kind="session",
                owner_id="session-1",
                fork=1,
                no_tools=True,
                timeout=60,
            ),
            _authority(),
        ),
        "fork",
    )


def test_standalone_ownership_is_explicit() -> None:
    request = _request(
        owner={"kind": "standalone", "profile": "task-assessor"},
        fork=False,
    )
    authority = _authority(
        owner_kind="standalone",
        owner_id="task-assessor",
        resume_sid=None,
    )
    admitted = admit_headless_request(request, authority).to_dict()
    assert admitted["owner"] == {
        "kind": "standalone",
        "profile": "task-assessor",
    }


def test_admission_freezes_authority_without_secrets() -> None:
    provider = _provider("claude")
    routing = {"node_id": "primary", "lane": ["headless"]}
    authority = _authority(provider=provider, routing=routing)
    admitted = admit_headless_request(_request(), authority)
    before = admitted.to_dict()

    provider["revision"] = 99
    provider["api_key"] = "late-secret"
    routing["node_id"] = "attacker"
    routing["lane"].append("mutated")

    assert admitted.to_dict() == before
    encoded = repr(admitted) + str(admitted.to_dict())
    assert "late-secret" not in encoded
    assert before["provider"]["revision"] == 7
    assert before["permission_scope"] == "spawn_runs"
    assert before["routing"] == {"lane": ["headless"], "node_id": "primary"}
    assert AdmittedHeadlessRequest.from_dict(before).to_dict() == before
    tampered = dict(before)
    tampered["model"] = "attacker-model"
    _raises(
        lambda: AdmittedHeadlessRequest.from_dict(tampered),
        "fingerprint",
    )


def test_secret_bearing_authority_is_rejected() -> None:
    _raises(
        lambda: _authority(provider={**_provider("claude"), "api_key": "secret"}),
        "secret-free",
    )
    _raises(
        lambda: _authority(routing={"authorization": "Bearer secret"}),
        "secret-free",
    )


def test_owner_and_capabilities_fail_truthfully() -> None:
    _raises(
        lambda: admit_headless_request(
            _request(),
            _authority(owner_id="different-session"),
        ),
        "owner",
    )
    _raises(
        lambda: admit_headless_request(
            _request(),
            _authority(supports_fork=False),
        ),
        "fork",
    )
    _raises(
        lambda: admit_headless_request(
            _request(),
            _authority(supports_no_tools=False),
        ),
        "no-tools",
    )


def test_provider_parity_contract() -> None:
    shapes = []
    for kind in (
        "agy",
        "amp",
        "claude",
        "codex",
        "copilot",
        "cursor",
        "kimi",
        "openai",
        "opencode",
        "pi",
        "qwen",
        "remote",
    ):
        admitted = admit_headless_request(_request(), _authority(kind))
        payload = admitted.to_dict()
        shapes.append(set(payload))
        assert payload["provider"]["kind"] == kind
        assert payload["model"] == "model-1"
        assert payload["reasoning_effort"] == "high"
        assert payload["runner"] == "subscription"
        assert payload["permission_scope"] == "spawn_runs"
        assert payload["routing"]["node_id"] == "primary"
        assert payload["fork"] is True
        assert payload["no_tools"] is True
    assert all(shape == shapes[0] for shape in shapes)


def main() -> None:
    try:
        test_request_schema_is_exact_and_non_coercive()
        test_standalone_ownership_is_explicit()
        test_admission_freezes_authority_without_secrets()
        test_secret_bearing_authority_is_rejected()
        test_owner_and_capabilities_fail_truthfully()
        test_provider_parity_contract()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print("headless request contract: ok")


if __name__ == "__main__":
    main()

"""Pre-provider runner failure classification.

Locks the bootstrap-handshake failure contract: a dead/missing runtime
bootstrap broker raises RuntimeBootstrapUnavailable (not a generic OSError
mislabeled as an input.json read failure), and fail_payload stamps the
machine-readable error_kind + pre_provider markers recovery relies on.

Run: cd backend && .venv/bin/python scripts/test_runner_bootstrap_classification.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home

_HOME = _test_home.isolate("runner-bootstrap-classification")

import runner_errors  # noqa: E402
from runner_operation_host import hydrate_runner_inputs  # noqa: E402


def test_fail_payload_marks_pre_provider() -> None:
    payload = runner_errors.fail_payload(
        "boom",
        error_kind=runner_errors.ERROR_KIND_BOOTSTRAP_UNAVAILABLE,
        pre_provider=True,
    )
    assert payload["success"] is False
    assert payload["session_id"] is None
    assert payload["error"] == "boom"
    assert payload["error_kind"] == "runtime_bootstrap_unavailable"
    assert payload["pre_provider"] is True


def test_fail_payload_default_has_no_markers() -> None:
    payload = runner_errors.fail_payload("boom")
    assert "error_kind" not in payload
    assert "pre_provider" not in payload


def test_missing_bootstrap_env_is_classified() -> None:
    os.environ.pop("BETTER_AGENT_RUNTIME_BOOTSTRAP", None)
    os.environ.pop("BETTER_CLAUDE_RUNTIME_BOOTSTRAP", None)
    with tempfile.TemporaryDirectory() as td:
        try:
            hydrate_runner_inputs({}, Path(td))
        except runner_errors.RuntimeBootstrapUnavailable:
            pass
        else:
            raise AssertionError("expected RuntimeBootstrapUnavailable")


def test_dead_broker_socket_is_classified() -> None:
    with tempfile.TemporaryDirectory() as td:
        # A socket path nothing listens on -> ECONNREFUSED on connect,
        # exactly what a retired one-shot broker produces.
        import socket

        sock_path = os.path.join(td, "dead.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.close()  # bound but never listening
        os.environ["BETTER_AGENT_RUNTIME_BOOTSTRAP"] = f"unix:{sock_path}"
        try:
            hydrate_runner_inputs({}, Path(td))
        except runner_errors.RuntimeBootstrapUnavailable as e:
            assert "refused" in str(e).lower() or "connect" in str(e).lower(), e
        else:
            raise AssertionError("expected RuntimeBootstrapUnavailable")
        finally:
            os.environ.pop("BETTER_AGENT_RUNTIME_BOOTSTRAP", None)


def main() -> int:
    tests = [
        test_fail_payload_marks_pre_provider,
        test_fail_payload_default_has_no_markers,
        test_missing_bootstrap_env_is_classified,
        test_dead_broker_socket_is_classified,
    ]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

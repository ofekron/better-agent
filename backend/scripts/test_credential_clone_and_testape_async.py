from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-credential-testape-async-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402

import config_store  # noqa: E402
import credential_clone_api  # noqa: E402
import testape_api  # noqa: E402
import testape_login_detector as detector  # noqa: E402
import testape_chat_panel_detector as chat_detector  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _spy_to_thread():
    """Patch asyncio.to_thread to record which callables it offloads,
    while still executing the real work."""
    original = asyncio.to_thread
    calls: list = []

    async def spy(fn, *args, **kwargs):
        calls.append(fn)
        return await original(fn, *args, **kwargs)

    asyncio.to_thread = spy
    return calls, original


def test_routes_are_async_def() -> None:
    assert inspect.iscoroutinefunction(credential_clone_api.clone_provider_credential)
    assert inspect.iscoroutinefunction(testape_api.get_login_state)
    assert inspect.iscoroutinefunction(testape_api.validate_chat_panel)


def test_clone_provider_credential_offloads_and_matches_prior_behavior() -> None:
    credential_clone_api.configure(lambda token: token == "good-token")
    original = config_store.clone_provider_credential
    entered = threading.Event()
    config_store.clone_provider_credential = lambda source, target: (
        entered.set(), "available",
    )[1]
    calls, original_to_thread = _spy_to_thread()
    try:
        body = credential_clone_api.CloneProviderCredentialRequest(
            source_provider_id="src", target_provider_id="tgt",
        )
        result = asyncio.run(credential_clone_api.clone_provider_credential(
            body, x_internal_token="good-token",
        ))
        assert result == {"status": "available"}
        assert entered.is_set(), "clone_provider_credential body never ran"
        assert config_store.clone_provider_credential in calls, (
            "config_store.clone_provider_credential must be offloaded via asyncio.to_thread"
        )
    finally:
        config_store.clone_provider_credential = original
        asyncio.to_thread = original_to_thread


def test_clone_provider_credential_error_mapping_preserved() -> None:
    credential_clone_api.configure(lambda token: token == "good-token")
    original = config_store.clone_provider_credential

    for outcome, expected_status in (
        ("missing", 404),
        ("blocked", 503),
    ):
        config_store.clone_provider_credential = lambda source, target, o=outcome: o
        try:
            body = credential_clone_api.CloneProviderCredentialRequest(
                source_provider_id="src", target_provider_id="tgt",
            )
            try:
                asyncio.run(credential_clone_api.clone_provider_credential(
                    body, x_internal_token="good-token",
                ))
            except HTTPException as exc:
                assert exc.status_code == expected_status, (outcome, exc.status_code)
            else:
                raise AssertionError(f"expected HTTPException for outcome={outcome!r}")
        finally:
            config_store.clone_provider_credential = original

    def raise_runtime_error(source, target):
        raise RuntimeError("credential authority down")

    config_store.clone_provider_credential = raise_runtime_error
    try:
        body = credential_clone_api.CloneProviderCredentialRequest(
            source_provider_id="src", target_provider_id="tgt",
        )
        try:
            asyncio.run(credential_clone_api.clone_provider_credential(
                body, x_internal_token="good-token",
            ))
        except HTTPException as exc:
            assert exc.status_code == 503
        else:
            raise AssertionError("expected HTTPException(503) on RuntimeError")
    finally:
        config_store.clone_provider_credential = original


def test_clone_provider_credential_rejects_invalid_token() -> None:
    credential_clone_api.configure(lambda token: False)
    body = credential_clone_api.CloneProviderCredentialRequest(
        source_provider_id="src", target_provider_id="tgt",
    )
    try:
        asyncio.run(credential_clone_api.clone_provider_credential(
            body, x_internal_token="bad-token",
        ))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected HTTPException(403) for invalid token")


class _StubResult:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


def test_get_login_state_offloads_detector_call_and_matches_result() -> None:
    original = detector.detect_login_state
    expected = {"state": "authenticated", "adapter_id": "a1"}
    entered = threading.Event()

    def fake_detect_login_state(*, adapter_id, url, fs_url):
        entered.set()
        return _StubResult(expected)

    detector.detect_login_state = fake_detect_login_state
    calls, original_to_thread = _spy_to_thread()
    try:
        result = asyncio.run(testape_api.get_login_state(
            adapter_id=None, url=None, fs_url=None,
        ))
        assert result == expected
        assert entered.is_set()
        assert fake_detect_login_state in calls, (
            "detector.detect_login_state must be offloaded via asyncio.to_thread"
        )
    finally:
        detector.detect_login_state = original
        asyncio.to_thread = original_to_thread


def test_get_login_state_error_mapping_preserved() -> None:
    original = detector.detect_login_state

    detector.detect_login_state = lambda **kw: (_ for _ in ()).throw(ValueError("bad adapter"))
    try:
        try:
            asyncio.run(testape_api.get_login_state(adapter_id=None, url=None, fs_url=None))
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("expected HTTPException(400)")
    finally:
        detector.detect_login_state = original

    detector.detect_login_state = lambda **kw: (_ for _ in ()).throw(RuntimeError("adapter down"))
    try:
        try:
            asyncio.run(testape_api.get_login_state(adapter_id=None, url=None, fs_url=None))
        except HTTPException as exc:
            assert exc.status_code == 502
        else:
            raise AssertionError("expected HTTPException(502)")
    finally:
        detector.detect_login_state = original


def test_validate_chat_panel_offloads_and_matches_result() -> None:
    original = chat_detector.validate_chat_panel
    expected = {"ok": True, "session_id": "s1"}
    entered = threading.Event()

    def fake_validate_chat_panel(*, adapter_id, session_id, url, fs_url):
        entered.set()
        return _StubResult(expected)

    chat_detector.validate_chat_panel = fake_validate_chat_panel
    calls, original_to_thread = _spy_to_thread()
    try:
        result = asyncio.run(testape_api.validate_chat_panel(
            adapter_id=None, session_id="s1", url=None, fs_url=None,
        ))
        assert result == expected
        assert entered.is_set()
        assert fake_validate_chat_panel in calls
    finally:
        chat_detector.validate_chat_panel = original
        asyncio.to_thread = original_to_thread


def test_validate_chat_panel_not_ok_raises_409() -> None:
    original = chat_detector.validate_chat_panel
    chat_detector.validate_chat_panel = lambda **kw: _StubResult({"ok": False, "reason": "mismatch"})
    try:
        try:
            asyncio.run(testape_api.validate_chat_panel(
                adapter_id=None, session_id="s1", url=None, fs_url=None,
            ))
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail == {"ok": False, "reason": "mismatch"}
        else:
            raise AssertionError("expected HTTPException(409) for ok=False result")
    finally:
        chat_detector.validate_chat_panel = original


def main() -> int:
    tests = [
        ("routes are async def", test_routes_are_async_def),
        ("clone_provider_credential offloads + matches prior behavior", test_clone_provider_credential_offloads_and_matches_prior_behavior),
        ("clone_provider_credential error mapping preserved", test_clone_provider_credential_error_mapping_preserved),
        ("clone_provider_credential rejects invalid token", test_clone_provider_credential_rejects_invalid_token),
        ("get_login_state offloads detector call + matches result", test_get_login_state_offloads_detector_call_and_matches_result),
        ("get_login_state error mapping preserved", test_get_login_state_error_mapping_preserved),
        ("validate_chat_panel offloads + matches result", test_validate_chat_panel_offloads_and_matches_result),
        ("validate_chat_panel not-ok raises 409", test_validate_chat_panel_not_ok_raises_409),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            print(f"  exception: {exc!r}")
            print(f"{FAIL} {name}")
            failures += 1
        else:
            print(f"{PASS} {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

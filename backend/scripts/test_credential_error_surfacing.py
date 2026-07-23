"""Credential turn-failure surfacing regression tests.

Locks the structured credential-error path end to end at its two
authoritative seams:
  1. ProviderCredentialError carries provider_id + credential_status and
     shapes error_meta() for the turn-failure funnel.
  2. session_manager.mark_user_error persists errorMeta on the user msg
     (what the in-chat credential-fix card renders from) and clears it
     on meta-less errors.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home  # noqa: E402
_test_home.isolate("ba-test-cred-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_manager  # noqa: E402
from provider import Provider, ProviderCredentialError  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


class _StubProvider(Provider):
    KIND = "stub"
    uses_managed_api_key = True

    def build_env(self):  # pragma: no cover - unused
        raise NotImplementedError

    def start_run(self, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError

    def _write_backend_state(self, rs):  # pragma: no cover - unused
        raise NotImplementedError

    def recover_in_flight(self, *a, **k):  # pragma: no cover - unused
        raise NotImplementedError

    def prune_old_runs(self, max_age_days: int = 7) -> int:  # pragma: no cover
        raise NotImplementedError

    async def run_headless(self, *a, **k):  # pragma: no cover - unused
        raise NotImplementedError

    async def rewind(self, *a, **k):  # pragma: no cover - unused
        raise NotImplementedError


def test_credential_error_carries_meta() -> None:
    p = _StubProvider({"id": "prov-1", "mode": "api_key"})
    try:
        p.require_runtime_credential()
        check(False, "require_runtime_credential raises without authority")
        return
    except ProviderCredentialError as e:
        check(e.provider_id == "prov-1", "exception carries provider_id")
        check(
            isinstance(e.credential_status, str) and bool(e.credential_status),
            "exception carries a credential_status",
        )
        meta = e.error_meta()
        check(meta["kind"] == "provider_credential", "error_meta kind is provider_credential")
        check(meta["provider_id"] == "prov-1", "error_meta carries provider_id")
        check(meta["credential_status"] == e.credential_status, "error_meta mirrors status")


def test_mark_user_error_persists_meta() -> None:
    sid = session_manager.manager.create(
        name="cred", cwd="/tmp", orchestration_mode="native",
    )["id"]
    try:
        appended = session_manager.manager.append_user_msg(
            sid, {"id": "u1", "role": "user", "content": "hi"},
        )
        check(appended is not None, "user msg appended")
        meta = {
            "kind": "provider_credential",
            "provider_id": "prov-1",
            "credential_status": "blocked",
        }
        session_manager.manager.mark_user_error(sid, "u1", "boom", meta=meta)
        msg = next(
            m for m in session_manager.manager.get(sid)["messages"]
            if m["id"] == "u1"
        )
        check(msg["status"] == "error", "msg marked error")
        check(msg.get("errorMeta") == meta, "errorMeta persisted on user msg")

        session_manager.manager.mark_user_error(sid, "u1", "other failure")
        msg = next(
            m for m in session_manager.manager.get(sid)["messages"]
            if m["id"] == "u1"
        )
        check("errorMeta" not in msg, "meta-less re-error clears stale errorMeta")
    finally:
        session_manager.manager.delete(sid)


def main() -> int:
    for fn in (test_credential_error_carries_meta, test_mark_user_error_persists_meta):
        print(f"\n{fn.__name__}:")
        fn()
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nAll credential-error surfacing checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

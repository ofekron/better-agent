"""Regression tests for the pre-send advisory seam.

Pins the core contract:

  1. Manifest validation accepts ``entrypoints.hooks.pre_send_advisory``
     (backend required, path must start with /) and still rejects
     unknown hook keys.
  2. ``pre_send_advisory_hooks`` enumerates only active, runtime-ready
     extensions declaring the hook.
  3. ``collect_pre_send_advisories`` normalizes extension responses:
     malformed advisories are dropped, percents are clamped, text is
     truncated, extension_id is stamped, one failing extension never
     drops another's advisories, and no hooks -> [] without any
     extension invocation.

Run with:
    cd backend && .venv/bin/python scripts/test_pre_send_advisory.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths

_TMP = tempfile.mkdtemp(prefix="ba_pre_send_advisory_test_")
paths.engage_test_home(_TMP)

import extension_store
import pre_send_advisory
from extension_store import ExtensionError, _validate_hooks
from fastapi import Response


def test_validate_hooks_accepts_pre_send_advisory() -> None:
    hooks = _validate_hooks({"pre_send_advisory": "/pre-send-advisory"}, has_backend=True)
    assert hooks == {"pre_send_advisory": "/pre-send-advisory"}

    try:
        _validate_hooks({"pre_send_advisory": "/p"}, has_backend=False)
        raise AssertionError("expected ExtensionError without backend")
    except ExtensionError:
        pass

    try:
        _validate_hooks({"pre_send_advisory": "no-slash"}, has_backend=True)
        raise AssertionError("expected ExtensionError for non-/ path")
    except ExtensionError:
        pass

    try:
        _validate_hooks({"bogus_hook": "/x"}, has_backend=True)
        raise AssertionError("expected ExtensionError for unknown hook key")
    except ExtensionError:
        pass
    print("ok test_validate_hooks_accepts_pre_send_advisory")


def test_hook_enumerator_filters_records() -> None:
    def fake_record(ext_id: str, path: str | None):
        entrypoints = {"hooks": {"pre_send_advisory": path} if path else {}}
        return {"manifest": {"id": ext_id, "entrypoints": entrypoints,
                             "permissions": {"backend_routes": True}}}

    records = [
        fake_record("a.with-hook", "/advise"),
        fake_record("b.no-hook", None),
        fake_record("c.inactive", "/advise"),
    ]
    orig = (
        extension_store.list_extensions,
        extension_store._record_active,
        extension_store._record_runtime_ready,
    )
    extension_store.list_extensions = lambda: records
    extension_store._record_active = lambda r: r["manifest"]["id"] != "c.inactive"
    extension_store._record_runtime_ready = lambda r: True
    try:
        hooks = extension_store.pre_send_advisory_hooks()
    finally:
        (
            extension_store.list_extensions,
            extension_store._record_active,
            extension_store._record_runtime_ready,
        ) = orig
    assert hooks == [("a.with-hook", "/advise")], hooks
    print("ok test_hook_enumerator_filters_records")


def test_collect_normalizes_and_isolates_failures() -> None:
    calls: list[tuple[str, str, dict]] = []

    async def fake_invoke(extension_id: str, path: str, *, method="POST", body_bytes=b"", base_url=""):
        calls.append((extension_id, path, json.loads(body_bytes)))
        if extension_id == "bad.extension":
            raise RuntimeError("subprocess died")
        payload = {
            "advisories": [
                {"title": "Claude — Session (5h): 87% of quota used",
                 "severity": "warn", "usage_percent": 187.5,
                 "resets_at": "2026-07-08T12:00:00Z", "detail": "d" * 1000},
                {"title": "", "severity": "warn"},
                {"title": "bad severity", "severity": "explode"},
                "not-a-dict",
            ]
        }
        return Response(content=json.dumps(payload), media_type="application/json")

    orig_hooks = extension_store.pre_send_advisory_hooks
    orig_invoke = pre_send_advisory.invoke_extension_backend
    extension_store.pre_send_advisory_hooks = lambda: [
        ("good.extension", "/advise"),
        ("bad.extension", "/advise"),
    ]
    pre_send_advisory.invoke_extension_backend = fake_invoke
    try:
        advisories = asyncio.run(
            pre_send_advisory.collect_pre_send_advisories("sid-1", "prov-1", "claude", "~/.claude", "opus")
        )
    finally:
        extension_store.pre_send_advisory_hooks = orig_hooks
        pre_send_advisory.invoke_extension_backend = orig_invoke

    assert len(advisories) == 1, advisories
    advisory = advisories[0]
    assert advisory["extension_id"] == "good.extension"
    assert advisory["usage_percent"] == 100.0
    assert len(advisory["detail"]) == 500
    assert advisory["resets_at"] == "2026-07-08T12:00:00Z"
    assert len(calls) == 2
    assert calls[0][2] == {
        "app_session_id": "sid-1",
        "provider_id": "prov-1",
        "provider_kind": "claude",
        "config_dir": "~/.claude",
        "model": "opus",
    }
    print("ok test_collect_normalizes_and_isolates_failures")


def test_collect_no_hooks_short_circuits() -> None:
    async def explode(*args, **kwargs):
        raise AssertionError("must not invoke any extension when no hooks declared")

    orig_hooks = extension_store.pre_send_advisory_hooks
    orig_invoke = pre_send_advisory.invoke_extension_backend
    extension_store.pre_send_advisory_hooks = lambda: []
    pre_send_advisory.invoke_extension_backend = explode
    try:
        advisories = asyncio.run(
            pre_send_advisory.collect_pre_send_advisories("sid", "prov", "claude", "", "m")
        )
    finally:
        extension_store.pre_send_advisory_hooks = orig_hooks
        pre_send_advisory.invoke_extension_backend = orig_invoke
    assert advisories == []
    print("ok test_collect_no_hooks_short_circuits")


if __name__ == "__main__":
    try:
        test_validate_hooks_accepts_pre_send_advisory()
        test_hook_enumerator_filters_records()
        test_collect_normalizes_and_isolates_failures()
        test_collect_no_hooks_short_circuits()
        print("ALL PRE-SEND ADVISORY TESTS PASSED")
    finally:
        import shutil

        shutil.rmtree(_TMP, ignore_errors=True)

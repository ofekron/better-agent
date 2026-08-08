from __future__ import annotations

import asyncio
import json
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-extension-projection-", provider="claude")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_api  # noqa: E402
import extension_store  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_serialize_projection_content_matches_json_dumps() -> None:
    value = {"b": 2, "a": [1, 2, 3], "c": None}
    content = extension_api._serialize_projection_content(value)
    assert isinstance(content, bytes)
    assert json.loads(content) == value
    # Exact byte-for-byte match with the compact separators the endpoints rely on.
    expected = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")
    assert content == expected


def test_build_and_serialize_projection_runs_build_then_serializes() -> None:
    calls: list[int] = []

    def build() -> dict:
        calls.append(1)
        return {"ok": True, "n": len(calls)}

    content = extension_api._build_and_serialize_projection(build)
    assert len(calls) == 1, "build must run exactly once"
    assert content == extension_api._serialize_projection_content({"ok": True, "n": 1})


def test_projection_cache_put_uses_shared_serializer_and_store() -> None:
    extension_api._projection_response_cache.clear()
    response = extension_api._projection_response_cache_put(
        "unit-test", ("key",), {"hello": "world"},
    )
    assert response.media_type == "application/json"
    assert json.loads(response.body) == {"hello": "world"}
    cached = extension_api._projection_response_cache_get("unit-test", ("key",))
    assert cached is not None
    assert json.loads(cached.body) == {"hello": "world"}


def test_threaded_projection_helper_builds_once_and_reuses_cache() -> None:
    extension_api._projection_response_cache.clear()
    build_calls: list[int] = []

    def build() -> dict:
        build_calls.append(1)
        return {"count": len(build_calls)}

    async def scenario() -> None:
        response1 = await extension_api._cached_json_projection_response_threaded(
            "threaded-unit-test", lambda: ("only-key",), build,
        )
        response2 = await extension_api._cached_json_projection_response_threaded(
            "threaded-unit-test", lambda: ("only-key",), build,
        )
        assert json.loads(response1.body) == {"count": 1}
        assert json.loads(response2.body) == {"count": 1}, "second call must be served from cache"
        assert len(build_calls) == 1, f"build ran {len(build_calls)} times; expected exactly once"

    asyncio.run(scenario())


def test_threaded_projection_concurrent_callers_share_one_build() -> None:
    extension_api._projection_response_cache.clear()
    build_calls: list[int] = []

    def build() -> dict:
        build_calls.append(1)
        return {"n": len(build_calls)}

    async def scenario() -> None:
        results = await asyncio.gather(*[
            extension_api._cached_json_projection_response_threaded(
                "threaded-concurrent-unit-test", lambda: ("shared-key",), build,
            )
            for _ in range(6)
        ])
        bodies = {json.loads(r.body)["n"] for r in results}
        assert bodies == {1}
        assert len(build_calls) == 1, f"build ran {len(build_calls)} times under concurrent callers"

    asyncio.run(scenario())


def test_list_extensions_build_and_serialize_matches_reconciliation() -> None:
    original = extension_store.list_extensions_with_reconciliation
    stub_extensions = [{"id": "stub-one"}]
    extension_store.list_extensions_with_reconciliation = (
        lambda *, include_hidden: (stub_extensions, True)
    )
    try:
        content, changed = extension_api._list_extensions_build_and_serialize(False)
    finally:
        extension_store.list_extensions_with_reconciliation = original

    assert changed is True
    assert content == extension_api._serialize_projection_content({"extensions": stub_extensions})


def test_list_extensions_route_matches_direct_reconciliation_call() -> None:
    # `list_extensions_with_reconciliation` is not idempotent across repeated
    # calls (reconciliation can rewrite timestamps/state), so pin it to a
    # deterministic stub rather than comparing two live invocations.
    extension_api._projection_response_cache.clear()
    extension_store._STORE_FINGERPRINT_CACHE = None
    original = extension_store.list_extensions_with_reconciliation
    stub_extensions = [{"id": "route-parity-stub"}]
    extension_store.list_extensions_with_reconciliation = (
        lambda *, include_hidden: (stub_extensions, False)
    )

    async def scenario() -> None:
        response = await extension_api.list_extensions(False)
        assert json.loads(response.body) == {"extensions": stub_extensions}

    try:
        asyncio.run(scenario())
    finally:
        extension_store.list_extensions_with_reconciliation = original


def test_projection_executor_admission_control_not_removed() -> None:
    source = extension_api.__file__ and open(extension_api.__file__, encoding="utf-8").read()
    assert '_EXTENSION_PROJECTION_EXECUTOR = BoundedAsyncExecutor(' in source
    assert 'name="extension.projection"' in source
    # The old two-hop pattern (build off-loop, then dumps+encode back on the
    # loop) must be gone from the threaded helper.
    start = source.index("async def _cached_json_projection_response_threaded(")
    end = source.index("def _list_extensions_build_and_serialize(", start)
    threaded_source = source[start:end]
    assert "await asyncio.to_thread(build)" not in threaded_source
    assert "await asyncio.to_thread(_build_and_serialize_projection, build)" in threaded_source


def main() -> int:
    tests = [
        ("serialize_projection_content matches json.dumps", test_serialize_projection_content_matches_json_dumps),
        ("build_and_serialize_projection runs build then serializes", test_build_and_serialize_projection_runs_build_then_serializes),
        ("projection cache put uses shared serializer+store", test_projection_cache_put_uses_shared_serializer_and_store),
        ("threaded projection helper builds once and reuses cache", test_threaded_projection_helper_builds_once_and_reuses_cache),
        ("threaded projection concurrent callers share one build", test_threaded_projection_concurrent_callers_share_one_build),
        ("list_extensions build_and_serialize matches reconciliation", test_list_extensions_build_and_serialize_matches_reconciliation),
        ("list_extensions route matches direct reconciliation call", test_list_extensions_route_matches_direct_reconciliation_call),
        ("projection executor admission control not removed", test_projection_executor_admission_control_not_removed),
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

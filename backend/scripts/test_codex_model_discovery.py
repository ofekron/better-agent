from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-codex-discovery-")

import config_store  # noqa: E402
from codex_model_discovery import (  # noqa: E402
    CatalogDiscoveryError,
    discover_models,
    inspect_catalog_source,
    write_discovery_cache,
)
from model_catalog_cache import read_catalog_cache  # noqa: E402
from scripts.codex_model_discovery_test_support import (  # noqa: E402
    codex_on_path as _codex_on_path,
    config_root as _config_root,
    environment as _environment,
    package_name as _package_name,
    provider as _provider,
    visible_catalog as _visible_catalog,
    write_executable as _write_executable,
)


def test_native_discovery_is_authoritative_canonical_and_schema3() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = _config_root(root, 'profile = "work"\n')
            provider = _provider(root, config_dir=config)
            executable = root / "bin" / "codex"
            _write_executable(executable, _visible_catalog())
            cache = root / "models.json"

            with _codex_on_path(executable):
                inspection = await inspect_catalog_source(provider["id"])
                assert inspection.status == "available", inspection
                result = await discover_models(
                    provider["id"],
                    expected_authority=inspection.authority,
                )

            assert result.status == "ok", result
            assert result.models == ("alpha", "zeta")
            assert result.authority is not None
            contract = result.authority.execution_contract
            assert dict(contract.environment_selectors) == {
                "CODEX_HOME": str(config.resolve()),
            }
            assert str((config / "config.toml").resolve()) in {
                identity.config_path for identity in contract.config
            }
            with _codex_on_path(executable):
                assert write_discovery_cache(
                    cache,
                    result,
                    retired=(),
                    refreshed_at=123.5,
                )
            persisted = read_catalog_cache(
                cache,
                expected_authority=result.authority,
            )
            assert persisted.status == "matching", persisted
            assert persisted.snapshot is not None
            assert persisted.snapshot.models == ("alpha", "zeta")
            encoded = json.dumps(persisted.snapshot.to_dict(), sort_keys=True)
            assert "gpt-5.6-sol" not in encoded
            assert "api_key" not in encoded

    asyncio.run(scenario())


def test_vendor_binary_and_ambient_codex_home_are_exact() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ambient = _config_root(root / "ambient", "ambient = true\n")
            provider = _provider(
                root / "provider",
                use_ambient_config=True,
            )
            package = root / "node_modules" / "@openai" / "codex"
            launcher = package / "bin" / "codex.js"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("launcher", encoding="utf-8")
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
            vendor_name = "codex.exe" if os.name == "nt" else "codex"
            vendor = (
                package
                / "node_modules"
                / "@openai"
                / _package_name()
                / "vendor"
                / "runtime"
                / vendor_name
            )
            _write_executable(
                vendor,
                {"models": [{"slug": "vendor-model", "visibility": "list"}]},
            )
            logical = root / "bin" / "codex"
            logical.parent.mkdir()
            logical.symlink_to(launcher)

            with _environment("CODEX_HOME", str(ambient)):
                with _codex_on_path(logical):
                    result = await discover_models(provider["id"])

            assert result.status == "ok", result
            assert result.models == ("vendor-model",)
            assert result.authority is not None
            assert dict(
                result.authority.execution_contract.environment_selectors,
            ) == {"CODEX_HOME": str(ambient.resolve())}
            assert result.authority.execution_contract.launch_chain.mode == "vendor"
            assert (
                result.authority.execution_contract.launch_chain.argv_prefix
                == (str(vendor.resolve()),)
            )

    asyncio.run(scenario())


def test_fugu_requires_scoped_sakana_catalog_without_static_filtering() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            provider = _provider(root, kind="fugu")
            executable = root / "bin" / "codex"
            pid_file = root / "pid"
            _write_executable(
                executable,
                {
                    "models": [
                        {"slug": "fugu-next", "visibility": "list"},
                        {"slug": "fugu", "visibility": "list"},
                    ],
                },
                pid_file=pid_file,
            )
            with _codex_on_path(executable):
                result = await discover_models(provider["id"])
            assert result.status == "unsupported"
            assert result.reason == "fugu_catalog_unverifiable"
            assert result.models == ()
            assert result.authority is None
            assert not pid_file.exists()

    asyncio.run(scenario())


def test_failures_never_authorize_or_write() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cases = (
                ("empty", {"models": []}, 0),
                ("error", {"models": []}, 9),
                ("error", {"models": "invalid"}, 0),
                (
                    "error",
                    {
                        "models": [
                            {"slug": "unknown", "visibility": "future"},
                        ],
                    },
                    0,
                ),
                (
                    "error",
                    {
                        "models": [
                            {"slug": "same", "visibility": "list"}
                            for _ in range(10_001)
                        ],
                    },
                    0,
                ),
            )
            for expected, payload, exit_code in cases:
                case = root / f"case-{expected}-{exit_code}-{len(str(payload))}"
                provider = _provider(case)
                executable = case / "bin" / "codex"
                _write_executable(executable, payload, exit_code=exit_code)
                with _codex_on_path(executable):
                    result = await discover_models(provider["id"])
                assert result.status == expected
                assert result.models == ()
                assert result.authority is None
                try:
                    write_discovery_cache(
                        case / "cache.json",
                        result,
                        retired=(),
                        refreshed_at=1.0,
                    )
                except CatalogDiscoveryError:
                    pass
                else:
                    raise AssertionError("non-authoritative result was written")
                assert not (case / "cache.json").exists()

            flood = root / "flood"
            provider = _provider(flood)
            executable = flood / "bin" / "codex"
            _write_executable(
                executable,
                {},
                raw_output_bytes=4 * 1024 * 1024 + 1,
            )
            with _codex_on_path(executable):
                oversized = await discover_models(provider["id"])
            assert oversized.status == "error"
            assert oversized.reason == "output_too_large"
            assert oversized.authority is None

            unsupported = _provider(root / "unsupported", kind="claude")
            result = await discover_models(unsupported["id"])
            assert result.status == "unsupported"
            assert result.authority is None

            unavailable = await discover_models("missing-provider")
            assert unavailable.status == "unavailable"
            assert unavailable.authority is None

    asyncio.run(scenario())


def test_executable_config_and_provider_drift_are_discarded() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for mutation in ("config", "executable"):
                case = root / mutation
                provider = _provider(case)
                executable = case / "bin" / "codex"
                _write_executable(
                    executable,
                    {"models": [{"slug": "untrusted", "visibility": "list"}]},
                    mutate=mutation,
                )
                with _codex_on_path(executable):
                    result = await discover_models(provider["id"])
                assert result.status == "error"
                assert result.authority is None

            case = root / "transient-config"
            config = case / "config"
            config.mkdir(parents=True)
            provider = _provider(case, config_dir=config)
            executable = case / "bin" / "codex"
            _write_executable(
                executable,
                {"models": [{"slug": "transient", "visibility": "list"}]},
                mutate="config_transient",
            )
            with _codex_on_path(executable):
                result = await discover_models(provider["id"])
            assert result.status == "error"
            assert result.reason == "authority_changed", result
            assert result.authority is None

            case = root / "provider"
            provider = _provider(case)
            executable = case / "bin" / "codex"
            _write_executable(
                executable,
                {"models": [{"slug": "untrusted", "visibility": "list"}]},
                provider_drift=(
                    provider["id"],
                    provider["generation"],
                    provider["revision"],
                ),
            )
            with _codex_on_path(executable):
                result = await discover_models(provider["id"])
            assert result.status == "error"
            assert result.reason == "authority_changed", result
            assert result.authority is None

            case = root / "write-cas"
            provider = _provider(case)
            executable = case / "bin" / "codex"
            _write_executable(
                executable,
                {"models": [{"slug": "current", "visibility": "list"}]},
            )
            cache = case / "cache.json"
            with _codex_on_path(executable):
                discovered = await discover_models(provider["id"])
                assert discovered.status == "ok"
                assert write_discovery_cache(
                    cache,
                    discovered,
                    retired=(),
                    refreshed_at=1.0,
                )
            before = cache.read_bytes()
            config_store.update_provider(
                provider["id"],
                {"nickname": "new-authority"},
                expected_generation=provider["generation"],
                expected_revision=provider["revision"],
            )
            with _codex_on_path(executable):
                try:
                    write_discovery_cache(
                        cache,
                        discovered,
                        retired=(),
                        refreshed_at=2.0,
                    )
                except CatalogDiscoveryError:
                    pass
                else:
                    raise AssertionError("stale authority overwrote cache")
            assert cache.read_bytes() == before

    asyncio.run(scenario())


def test_timeout_is_bounded_off_event_loop() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            provider = _provider(root)
            executable = root / "bin" / "codex"
            pid_file = root / "pid"
            _write_executable(
                executable,
                {"models": [{"slug": "late", "visibility": "list"}]},
                delay_seconds=5,
                pid_file=pid_file,
            )
            started = time.monotonic()
            with _codex_on_path(executable):
                pending = asyncio.create_task(
                    discover_models(provider["id"], timeout_seconds=0.1),
                )
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.05)
                result = await asyncio.wait_for(pending, timeout=2)
            assert result.status == "error"
            assert result.reason == "timeout"
            assert result.authority is None
            assert time.monotonic() - started < 2
            from proc_control import process_control
            if pid_file.exists():
                pid = int(pid_file.read_text(encoding="utf-8"))
                assert not process_control().pid_alive(pid)

            retry_root = root / "retry"
            retry_provider = _provider(retry_root)
            retry_executable = retry_root / "bin" / "codex"
            retry_pid = retry_root / "pid"
            _write_executable(
                retry_executable,
                {"models": [{"slug": "late-retry", "visibility": "list"}]},
                mutate="stabilize_then_delay",
                pid_file=retry_pid,
            )
            started = time.monotonic()
            with _codex_on_path(retry_executable):
                retried = await discover_models(
                    retry_provider["id"],
                    timeout_seconds=0.3,
                )
            assert retried.status == "error"
            assert retried.reason == "timeout"
            assert time.monotonic() - started < 2
            if retry_pid.exists():
                assert not process_control().pid_alive(
                    int(retry_pid.read_text(encoding="utf-8")),
                )

    asyncio.run(scenario())


def test_credential_rotation_changes_non_secret_catalog_authority() -> None:
    async def scenario() -> None:
        credentials: dict[str, str] = {}
        provider_id = ""
        original_available = config_store.credential_session_client.available
        original_request = config_store.credential_session_client.request

        def request(action: str, provider_id: str, **kwargs) -> dict:
            current = credentials.get(provider_id, "")
            if action == "status":
                return {"status": "available" if current else "missing"}
            if action == "read":
                return {
                    "status": "available" if current else "missing",
                    **({"value": current} if current else {}),
                }
            if action == "compare_set":
                if current != kwargs["expected_value"]:
                    return {"status": "available", "applied": False}
                value = kwargs["value"]
                if value:
                    credentials[provider_id] = value
                    return {"status": "available", "applied": True}
                credentials.pop(provider_id, None)
                return {"status": "missing", "applied": True}
            raise AssertionError(f"unexpected credential action: {action}")

        config_store.credential_session_client.available = lambda: True
        config_store.credential_session_client.request = request
        try:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                config = _config_root(root)
                first_secret = "credential-rotation-secret-one"
                provider = config_store.add_provider({
                    "name": "Credential discovery",
                    "kind": "codex",
                    "mode": "api_key",
                    "config_dir": str(config),
                    "api_key": first_secret,
                })
                provider_id = provider["id"]
                executable = root / "bin" / "codex"
                _write_executable(
                    executable,
                    {
                        "models": [
                            {"slug": "account-model", "visibility": "list"},
                        ],
                    },
                    forbidden_environment_value=first_secret,
                )
                cache = root / "cache.json"
                with _codex_on_path(executable):
                    discovered = await discover_models(provider["id"])
                    assert discovered.status == "ok"
                    assert discovered.authority is not None
                    assert (
                        discovered.authority.execution_contract.credential_generation
                        == provider["revision"]
                    )
                    assert first_secret not in json.dumps(
                        discovered.authority.to_dict(),
                        sort_keys=True,
                    )
                    assert write_discovery_cache(
                        cache,
                        discovered,
                        retired=(),
                        refreshed_at=1.0,
                    )
                before = cache.read_bytes()
                rotated = config_store.update_provider(
                    provider["id"],
                    {"api_key": "credential-rotation-secret-two"},
                    expected_generation=provider["generation"],
                    expected_revision=provider["revision"],
                )
                assert rotated is not None
                assert rotated["revision"] == provider["revision"] + 1
                with _codex_on_path(executable):
                    try:
                        write_discovery_cache(
                            cache,
                            discovered,
                            retired=(),
                            refreshed_at=2.0,
                        )
                    except CatalogDiscoveryError:
                        pass
                    else:
                        raise AssertionError(
                            "credential rotation preserved stale authority",
                        )
                assert cache.read_bytes() == before
        finally:
            config_store.credential_session_client.available = original_available
            config_store.credential_session_client.request = original_request
            if provider_id:
                with config_store._api_key_cache_lock:
                    config_store._api_key_cache.pop(provider_id, None)
                config_store._credential_status.pop(provider_id, None)

    asyncio.run(scenario())


def test_cancellation_reaps_started_process_before_propagating() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            provider = _provider(root)
            executable = root / "bin" / "codex"
            pid_file = root / "pid"
            _write_executable(
                executable,
                {"models": [{"slug": "late", "visibility": "list"}]},
                delay_seconds=5,
                pid_file=pid_file,
            )
            with _codex_on_path(executable):
                pending = asyncio.create_task(
                    discover_models(provider["id"], timeout_seconds=10),
                )
                deadline = time.monotonic() + 2
                while not pid_file.exists():
                    if time.monotonic() >= deadline:
                        raise AssertionError("catalog process did not start")
                    await asyncio.sleep(0)
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancellation did not propagate")
            pid = int(pid_file.read_text(encoding="utf-8"))
            from proc_control import process_control
            assert not process_control().pid_alive(pid)

            retry_root = root / "retry"
            retry_provider = _provider(retry_root)
            retry_executable = retry_root / "bin" / "codex"
            retry_pid_file = retry_root / "pid"
            _write_executable(
                retry_executable,
                {"models": [{"slug": "late-retry", "visibility": "list"}]},
                mutate="stabilize_then_delay",
                pid_file=retry_pid_file,
            )
            with _codex_on_path(retry_executable):
                retried = asyncio.create_task(
                    discover_models(
                        retry_provider["id"],
                        timeout_seconds=10,
                    ),
                )
                deadline = time.monotonic() + 2
                while not retry_pid_file.exists():
                    if time.monotonic() >= deadline:
                        raise AssertionError("first catalog process did not start")
                    await asyncio.sleep(0)
                first_pid = 0
                while not first_pid:
                    if time.monotonic() >= deadline:
                        raise AssertionError("first catalog pid was unavailable")
                    raw_pid = retry_pid_file.read_text(encoding="utf-8")
                    first_pid = int(raw_pid) if raw_pid.isdigit() else 0
                    await asyncio.sleep(0)
                while True:
                    raw_pid = retry_pid_file.read_text(encoding="utf-8")
                    retry_pid = int(raw_pid) if raw_pid.isdigit() else first_pid
                    if retry_pid != first_pid:
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("retry catalog process did not start")
                    await asyncio.sleep(0)
                retried.cancel()
                try:
                    await retried
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("retry cancellation did not propagate")
            assert not process_control().pid_alive(retry_pid)

    asyncio.run(scenario())


TESTS = (
    test_native_discovery_is_authoritative_canonical_and_schema3,
    test_vendor_binary_and_ambient_codex_home_are_exact,
    test_fugu_requires_scoped_sakana_catalog_without_static_filtering,
    test_failures_never_authorize_or_write,
    test_executable_config_and_provider_drift_are_discarded,
    test_timeout_is_bounded_off_event_loop,
    test_credential_rotation_changes_non_secret_catalog_authority,
    test_cancellation_reaps_started_process_before_propagating,
)


if __name__ == "__main__":
    try:
        for test in TESTS:
            test()
    finally:
        TEST_HOME.release()
    print("PASS authoritative Codex model discovery")

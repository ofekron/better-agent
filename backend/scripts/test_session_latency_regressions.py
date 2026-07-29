from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-session-latency-")

import dependency_plan  # noqa: E402
import extension_store  # noqa: E402
from codex_model_discovery import inspect_catalog_source  # noqa: E402
from scripts.codex_model_discovery_test_support import (  # noqa: E402
    codex_on_path,
    config_root,
    provider,
    visible_catalog,
    write_executable,
)


def test_catalog_authority_covers_runtime_configuration() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = config_root(root, 'profile = "work"\n')
            (config / "AGENTS.md").write_text("runtime instructions", encoding="utf-8")
            agents = config / "agents"
            agents.mkdir()
            (agents / "reviewer.toml").write_text("name = 'reviewer'\n", encoding="utf-8")
            record = provider(root, config_dir=config)
            executable = root / "bin" / "codex"
            write_executable(executable, visible_catalog())

            with codex_on_path(executable):
                inspection = await inspect_catalog_source(record["id"])
                (config / "unrelated-state.json").write_text(
                    "{}",
                    encoding="utf-8",
                )
                refreshed = await inspect_catalog_source(record["id"])

            assert inspection.status == "available", inspection
            assert inspection.authority is not None
            assert refreshed.status == "available", refreshed
            assert refreshed.authority is not None
            assert (
                inspection.authority.execution_contract.fingerprint
                != refreshed.authority.execution_contract.fingerprint
            )
            assert (
                inspection.authority.execution_contract.catalog_fingerprint
                == refreshed.authority.execution_contract.catalog_fingerprint
            )
            paths = {
                identity.config_path
                for identity in inspection.authority.execution_contract.config
            }
            assert paths == {
                str((config / "AGENTS.md").resolve()),
                str((config / "agents" / "reviewer.toml").resolve()),
                str((config / "auth.json").resolve()),
                str((config / "config.toml").resolve()),
            }

    asyncio.run(scenario())


def test_extension_fingerprint_hashes_only_when_file_identity_changes() -> None:
    path = extension_store._store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version":2,"extensions":{},"deleted_extensions":{}}',
        encoding="utf-8",
    )
    extension_store._refresh_store_fingerprint_cache(path)
    time.sleep(extension_store._STORE_FINGERPRINT_TTL_SECONDS + 0.05)

    original = extension_store._fingerprint_store_file
    reads = 0

    def counted(candidate: Path):
        nonlocal reads
        reads += 1
        return original(candidate)

    with patch.object(extension_store, "_fingerprint_store_file", counted):
        extension_store.store_fingerprint()
        assert reads == 0
        path.write_text(
            '{"schema_version":2,"extensions":{"changed":{}},'
            '"deleted_extensions":{}}',
            encoding="utf-8",
        )
        time.sleep(extension_store._STORE_FINGERPRINT_TTL_SECONDS + 0.05)
        extension_store.store_fingerprint()
        assert reads == 1


def test_provider_module_probe_is_stable_for_process_lifetime() -> None:
    dependency_plan._module_available.cache_clear()
    calls = 0
    original = importlib.util.find_spec

    def counted(module: str):
        nonlocal calls
        calls += 1
        return original(module)

    with patch.object(importlib.util, "find_spec", counted):
        first = dependency_plan._module_available("json")
        second = dependency_plan._module_available("json")

    assert first is True
    assert second is True
    assert calls == 1


def test_runtime_agents_do_not_wait_for_extension_mutation_lock() -> None:
    extension_store._RUNTIME_AGENT_ENTRIES_CACHE = None
    with patch.object(
        extension_store,
        "_load",
        side_effect=AssertionError("runtime projection acquired mutation lock"),
    ):
        expected = extension_store.runtime_agent_entries()
    with patch.object(
        extension_store,
        "read_json",
        side_effect=AssertionError("runtime projection missed fingerprint cache"),
    ):
        assert extension_store.runtime_agent_entries() == expected


def test_model_admission_failure_bypasses_retry_backoff() -> None:
    source = (BACKEND / "orchestrator.py").read_text(encoding="utf-8")
    processor_start = source.index("async def _run_session_processor(")
    processor_end = source.index(
        "def _startup_recovery_can_overlap_session(",
        processor_start,
    )
    processor = source[processor_start:processor_end]
    permanent = processor.index("except ModelAdmissionError as e:")
    transient = processor.index("except Exception as e:", permanent)
    permanent_branch = processor[permanent:transient]
    assert "await consume_queue_item()" in permanent_branch
    assert "asyncio.sleep" not in permanent_branch
    assert permanent < transient


def test_model_admission_compares_stable_catalog_identity() -> None:
    source = (BACKEND / "model_execution_admission.py").read_text(
        encoding="utf-8",
    )
    admission_start = source.index("def _admit_catalog_model(")
    admission_end = source.index(
        "\ndef admit_execution_model(",
        admission_start,
    )
    admission = source[admission_start:admission_end]
    assert (
        "contract.catalog_fingerprint\n"
        "        != authority.execution_contract.catalog_fingerprint"
    ) in admission
    assert "replace(contract" not in admission


def test_turn_driver_receives_lifecycle_identity_explicitly() -> None:
    source = (BACKEND / "turn_manager.py").read_text(encoding="utf-8")
    call_start = source.index("primary_result = await self._drive_cli_run(")
    call_end = source.index("\n            )", call_start)
    call = source[call_start:call_end]
    assert "lifecycle_message_id=owning_lifecycle_message_id" in call

    driver_start = source.index("async def _drive_cli_run(")
    driver = source[driver_start:]
    assert "lifecycle_message_id: str" in driver[:1500]
    identity_start = driver.index("admission_identity = {")
    identity_end = driver.index("\n                    }", identity_start)
    identity = driver[identity_start:identity_end]
    assert '"user_turn_id": lifecycle_message_id' in identity
    assert '"lifecycle_message_id": lifecycle_message_id' in identity
    assert "owning_lifecycle_message_id" not in driver


if __name__ == "__main__":
    try:
        test_catalog_authority_covers_runtime_configuration()
        test_extension_fingerprint_hashes_only_when_file_identity_changes()
        test_provider_module_probe_is_stable_for_process_lifetime()
        test_runtime_agents_do_not_wait_for_extension_mutation_lock()
        test_model_admission_failure_bypasses_retry_backoff()
        test_model_admission_compares_stable_catalog_identity()
        test_turn_driver_receives_lifecycle_identity_explicitly()
        print("ALL PASS")
    finally:
        TEST_HOME.release()

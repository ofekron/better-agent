from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_test_home.isolate("ba-test-native-exposure-safety-")

import extension_mcp  # noqa: E402
import extension_store  # noqa: E402
import ambient_mcp_sources  # noqa: E402


def _record(extension_id: str, *, skill: str = "", mcp: str = "") -> dict:
    entrypoints: dict[str, list[dict]] = {}
    if skill:
        entrypoints["skills"] = [{"name": skill, "path": f"skills/{skill}"}]
    if mcp:
        entrypoints["mcp"] = [{
            "name": mcp,
            "command": mcp,
            "args": [],
            "env": {},
            "user_facing": False,
            "requires_backend_auth": False,
            "native_exposure": {"allowed": True, "permissions": []},
            "predicate": {},
        }]
    return {
        "manifest": {"id": extension_id, "name": extension_id, "entrypoints": entrypoints},
        "source": {"type": "git"},
        "enabled": True,
    }


def test_unowned_native_skill_is_never_overwritten() -> None:
    with tempfile.TemporaryDirectory(prefix="native-skill-collision-") as tmp:
        root = Path(tmp)
        source = root / "source"
        target = root / "reviewer"
        source.mkdir()
        target.mkdir()
        (source / "SKILL.md").write_text("extension\n", encoding="utf-8")
        original = target / "SKILL.md"
        original.write_text("personal\n", encoding="utf-8")

        try:
            extension_store._replace_runtime_skill_dir(source, target, "ofek.extension")
            raise AssertionError("unowned native skill was overwritten")
        except extension_store.ExtensionError:
            pass

        assert original.read_text(encoding="utf-8") == "personal\n"
        assert not (target / extension_store._RUNTIME_SKILL_OWNER_FILE).exists()


def test_pcs_failure_rolls_back_native_exposure_state() -> None:
    record = _record("ofek.failure", mcp="search")
    real_get = extension_store.get_extension
    real_reconcile = extension_store.reconcile_native_mcp_servers
    extension_store.get_extension = lambda extension_id: record if extension_id == "ofek.failure" else real_get(extension_id)  # type: ignore[assignment]
    calls = 0

    def fail_after_concurrent_update() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            concurrent = extension_store._load_ext_settings()
            other = extension_store._ext_settings_entry(concurrent, "ofek.concurrent")
            other["values"]["preserved"] = True
            extension_store._save_ext_settings(concurrent)
        raise OSError("PCS unavailable")

    extension_store.reconcile_native_mcp_servers = fail_after_concurrent_update  # type: ignore[assignment]
    try:
        try:
            extension_store.set_native_harness_exposed("ofek.failure", "mcp", "search", True)
            raise AssertionError("PCS failure was reported as success")
        except extension_store.ExtensionError:
            pass
        assert extension_store.native_harness_exposed(
            "ofek.failure", "mcp", "search", record=record
        ) is False
        concurrent = extension_store._load_ext_settings()
        assert concurrent["extensions"]["ofek.concurrent"]["values"]["preserved"] is True
    finally:
        extension_store.get_extension = real_get  # type: ignore[assignment]
        extension_store.reconcile_native_mcp_servers = real_reconcile  # type: ignore[assignment]


def test_concurrent_successful_exposure_updates_are_both_preserved() -> None:
    records = {
        "ofek.first": _record("ofek.first", mcp="first-search"),
        "ofek.second": _record("ofek.second", mcp="second-search"),
    }
    real_get = extension_store.get_extension
    real_load = extension_store._load_ext_settings
    real_reconcile = extension_store.reconcile_native_mcp_servers
    first_loaded = threading.Event()
    second_started = threading.Event()
    first_thread_id: int | None = None
    transaction_lock_observations: list[bool] = []

    def coordinated_load() -> dict:
        data = real_load()
        transaction_lock_observations.append(extension_store._EXT_SETTINGS_LOCK._is_owned())
        if threading.get_ident() == first_thread_id and not first_loaded.is_set():
            first_loaded.set()
            assert second_started.wait(timeout=2)
        return data

    extension_store.get_extension = lambda extension_id: records.get(extension_id) or real_get(extension_id)  # type: ignore[assignment]
    extension_store._load_ext_settings = coordinated_load  # type: ignore[assignment]
    extension_store.reconcile_native_mcp_servers = lambda: 0  # type: ignore[assignment]
    errors: list[BaseException] = []

    def expose(extension_id: str, server_name: str, *, second: bool = False) -> None:
        nonlocal first_thread_id
        try:
            if second:
                second_started.set()
            else:
                first_thread_id = threading.get_ident()
            extension_store.set_native_harness_exposed(extension_id, "mcp", server_name, True)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=expose, args=("ofek.first", "first-search"))
    second = threading.Thread(
        target=expose,
        args=("ofek.second", "second-search"),
        kwargs={"second": True},
    )
    try:
        first.start()
        assert first_loaded.wait(timeout=2)
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive() and not second.is_alive()
        assert not errors
        assert transaction_lock_observations and all(transaction_lock_observations)
        settings = real_load()
        assert settings["extensions"]["ofek.first"]["native_harness"] == ["mcp:first-search"]
        assert settings["extensions"]["ofek.second"]["native_harness"] == ["mcp:second-search"]
    finally:
        extension_store.get_extension = real_get  # type: ignore[assignment]
        extension_store._load_ext_settings = real_load  # type: ignore[assignment]
        extension_store.reconcile_native_mcp_servers = real_reconcile  # type: ignore[assignment]


def test_user_owned_native_mcp_name_collision_is_rejected() -> None:
    real_configure = extension_mcp._configure_pcs
    real_projection = ambient_mcp_sources.capabilities
    real_reconcile = extension_mcp._pcs.reconcile_global_mcp_servers
    captured: dict = {}

    def reconcile(desired, *, owns_server, providers=None):
        captured.update(desired)
        assert providers is not None
        assert owns_server("search", desired["search"])
        assert not owns_server("search", {"command": "personal-search"})
        raise ValueError("user-owned collision")

    extension_mcp._configure_pcs = lambda: None  # type: ignore[assignment]
    extension_launcher = extension_mcp.launcher_server_item("ofek.extension", "search")
    ambient_mcp_sources.capabilities = lambda: [ambient_mcp_sources.AmbientMcpCapability(
        id="extension:search", name="search", launcher=extension_launcher, policy={},
        ownership="extension", available=True,
    )]  # type: ignore[assignment]
    extension_mcp._pcs.reconcile_global_mcp_servers = reconcile  # type: ignore[assignment]
    try:
        try:
            extension_mcp.reconcile_native_mcp_servers([])
            raise AssertionError("user-owned MCP collision was accepted")
        except ValueError:
            pass
        assert list(captured) == ["search"]
    finally:
        extension_mcp._configure_pcs = real_configure  # type: ignore[assignment]
        ambient_mcp_sources.capabilities = real_projection  # type: ignore[assignment]
        extension_mcp._pcs.reconcile_global_mcp_servers = real_reconcile  # type: ignore[assignment]


def test_reconcile_uses_canonical_ambient_projection_once() -> None:
    real_configure = extension_mcp._configure_pcs
    real_projection = ambient_mcp_sources.capabilities
    real_reconcile = extension_mcp._pcs.reconcile_global_mcp_servers
    projection_calls = 0

    def projection():
        nonlocal projection_calls
        projection_calls += 1
        return [
            ambient_mcp_sources.AmbientMcpCapability(
                id="user:notes", name="notes", launcher={"command": "notes", "env": {}},
                policy={}, ownership="user", available=True,
            ),
            ambient_mcp_sources.AmbientMcpCapability(
                id="core:ui", name="ui", launcher=None, policy={},
                ownership="better-agent-core", available=False,
            ),
        ]

    extension_mcp._configure_pcs = lambda: None  # type: ignore[assignment]
    ambient_mcp_sources.capabilities = projection  # type: ignore[assignment]
    def reconcile(desired, *, owns_server, providers=None):
        assert providers is not None
        assert list(desired) == ["notes"]
        assert desired["notes"]["env"]["BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID"] == "user:notes"
        assert owns_server("notes", desired["notes"])
        return {"changed": ["claude", "codex", "gemini"]}
    extension_mcp._pcs.reconcile_global_mcp_servers = reconcile  # type: ignore[assignment]
    try:
        assert extension_mcp.reconcile_native_mcp_servers([_record("ignored", mcp="duplicate")]) == 3
        assert projection_calls == 1
    finally:
        extension_mcp._configure_pcs = real_configure  # type: ignore[assignment]
        ambient_mcp_sources.capabilities = real_projection  # type: ignore[assignment]
        extension_mcp._pcs.reconcile_global_mcp_servers = real_reconcile  # type: ignore[assignment]


def test_reconcile_filters_providers_without_global_mcp_adapter() -> None:
    real_configure = extension_mcp._configure_pcs
    real_projection = ambient_mcp_sources.capabilities
    real_reconcile = extension_mcp._pcs.reconcile_global_mcp_servers
    import config_store
    captured: dict[str, list[dict]] = {}

    def projection():
        return [
            ambient_mcp_sources.AmbientMcpCapability(
                id="user:notes", name="notes", launcher={"command": "notes", "env": {}},
                policy={}, ownership="user", available=True,
            ),
        ]

    def reconcile(desired, *, owns_server, providers=None):
        del desired, owns_server
        captured["providers"] = list(providers or [])
        return {"changed": []}

    extension_mcp._configure_pcs = lambda: None  # type: ignore[assignment]
    ambient_mcp_sources.capabilities = projection  # type: ignore[assignment]
    extension_mcp._pcs.reconcile_global_mcp_servers = reconcile  # type: ignore[assignment]
    original_metadata = config_store.list_provider_metadata
    config_store.list_provider_metadata = lambda: [  # type: ignore[assignment]
        {"id": "claude-1", "kind": "claude"},
        {"id": "copilot-1", "kind": "copilot"},
        {"id": "codex-1", "kind": "codex"},
    ]
    try:
        assert extension_mcp.reconcile_native_mcp_servers([]) == 0
        assert [provider["kind"] for provider in captured["providers"]] == ["claude", "codex"]
    finally:
        extension_mcp._configure_pcs = real_configure  # type: ignore[assignment]
        ambient_mcp_sources.capabilities = real_projection  # type: ignore[assignment]
        extension_mcp._pcs.reconcile_global_mcp_servers = real_reconcile  # type: ignore[assignment]
        config_store.list_provider_metadata = original_metadata  # type: ignore[assignment]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all native exposure safety tests passed")

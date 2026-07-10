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
            "ambient_native": True,
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
    active = extension_mcp._active_server_items([_record("ofek.extension", mcp="search")])
    capability = {"unified": {}, "specifics": []}
    real_content = extension_mcp._pcs._mcp_tool_content
    extension_mcp._pcs._mcp_tool_content = lambda _current, _exists: {
        "mcpServers": {"search": {"command": "personal-search"}}
    }
    try:
        try:
            extension_mcp._assert_entries_available(capability, {}, True, active)
            raise AssertionError("user-owned MCP collision was accepted")
        except ValueError:
            pass
    finally:
        extension_mcp._pcs._mcp_tool_content = real_content


def test_extension_native_mcp_name_collision_is_rejected() -> None:
    try:
        extension_mcp._active_server_items([
            _record("ofek.first", mcp="search"),
            _record("ofek.second", mcp="search"),
        ])
        raise AssertionError("extension MCP collision was accepted")
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all native exposure safety tests passed")

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import paths
from node_service import (
    LABEL,
    NodeServiceManager,
    ServiceSpec,
    attest_durable_topology,
    begin_launcher_attempt,
    macos_plist,
    mark_launcher_healthy,
    publish_launcher_status,
    read_status,
    require_durable_topology,
    windows_task_xml,
    _windows_projection_matches,
)


def _spec(tmp_path: Path) -> ServiceSpec:
    executable = tmp_path / "Better Agent"
    executable.write_text("binary", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    paths.make_private_directory(home)
    topology = home / "topology.yaml"
    topology.write_text("schema_version: 1\n", encoding="utf-8")
    paths.make_private_file(topology)
    return ServiceSpec(executable.resolve(), home.resolve(), topology.resolve())


def test_macos_plist_is_same_user_keepalive_contract(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    value = macos_plist(spec)
    assert value["Label"] == LABEL
    assert value["ProgramArguments"] == list(spec.command)
    assert value["RunAtLoad"] is True
    assert value["KeepAlive"] is True
    assert value["EnvironmentVariables"]["BETTER_AGENT_HOME"] == str(spec.state_root)
    assert value["EnvironmentVariables"]["BETTER_AGENT_TOPOLOGY_PATH"] == str(
        spec.topology_path
    )
    assert plistlib.loads(plistlib.dumps(value)) == value


def test_windows_xml_is_least_privilege_logon_and_bounded_restart(tmp_path: Path) -> None:
    xml = windows_task_xml(_spec(tmp_path), "S-1-5-21-100")
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<Interval>PT30S</Interval>" in xml
    assert "<Count>5</Count>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "--node-launcher" in xml


def test_crash_budget_persists_and_circuit_stays_open(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    for attempt in range(5):
        assert begin_launcher_attempt(spec.state_root, now=100 + attempt)
    assert not begin_launcher_attempt(spec.state_root, now=106)
    assert not begin_launcher_attempt(spec.state_root, now=500)
    mark_launcher_healthy(spec.state_root, now=500)
    assert not begin_launcher_attempt(spec.state_root, now=799)
    assert begin_launcher_attempt(spec.state_root, now=800)


def test_launcher_and_connection_status_are_composed(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    publish_launcher_status(spec.state_root, "running")
    status = read_status(spec.state_root)
    assert status["launcher"] == "running"
    assert status["connection"] == "unreachable"


def test_windows_enable_and_remove_are_idempotent_transactions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    calls: list[list[str]] = []
    registered = False

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal registered
        calls.append(command)
        if command[0] == "whoami":
            return subprocess.CompletedProcess(
                command, 0, '"DESKTOP\\User","S-1-5-21-100"\n', "",
            )
        if command[0] == "powershell.exe":
            if "Export-ScheduledTask" in command[-1]:
                stdout = windows_task_xml(spec, "S-1-5-21-100") if registered else ""
                return subprocess.CompletedProcess(
                    command, 0 if registered else 3, stdout, "",
                )
            return subprocess.CompletedProcess(command, 0, "3\n", "")
        if command[:2] == ["schtasks", "/Create"]:
            registered = True
        elif command[:2] == ["schtasks", "/Delete"]:
            registered = False
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("node_service.require_durable_topology", lambda *args: "wss://primary")
    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="win32",
        run=run,
        app_version="1",
    )
    assert manager.reconcile() == "enabled"
    assert manager.reconcile() == "enabled"
    manager.remove()
    assert manager.reconcile() == "removed"
    assert sum(call[:2] == ["schtasks", "/Create"] for call in calls) == 1
    assert ["schtasks", "/Run", "/TN", "\\Better Agent\\Node"] in calls
    assert ["schtasks", "/Delete", "/TN", "\\Better Agent\\Node", "/F"] in calls
    assert read_status(spec.state_root)["desired"] == "removed"


def test_invalid_windows_sid_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        windows_task_xml(_spec(tmp_path), "user name")


def test_durable_topology_requires_matching_tls_attestation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    topology = SimpleNamespace(
        primary=SimpleNamespace(address="wss://primary.example")
    )
    monkeypatch.setattr("node_service.load_topology", lambda: topology)
    with pytest.raises(RuntimeError):
        require_durable_topology(spec.state_root)
    attest_durable_topology("wss://primary.example", spec.state_root)
    assert require_durable_topology(spec.state_root) == "wss://primary.example"


@pytest.mark.parametrize(
    "address",
    (
        "wss://localhost:9443",
        "wss://localhost.:9443",
        "wss://foo.localhost:9443",
        "wss://127.0.0.1:9443",
        "wss://[::1]:9443",
    ),
)
def test_durable_topology_rejects_unowned_loopback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    address: str,
) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(
        "node_service.load_topology",
        lambda: SimpleNamespace(primary=SimpleNamespace(address=address)),
    )
    attest_durable_topology(address, spec.state_root)
    with pytest.raises(RuntimeError):
        require_durable_topology(spec.state_root)


def test_macos_reconcile_preserves_existing_service_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    calls: list[list[str]] = []
    launch_agent = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    registered = False

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal registered
        calls.append(command)
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                command,
                0 if registered else 113,
                "",
                "" if registered else "Could not find service",
            )
        if command[:2] == ["launchctl", "bootstrap"]:
            registered = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["launchctl", "bootout"]:
            registered = False
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "failed")

    monkeypatch.setattr("node_service.require_durable_topology", lambda *args: "wss://primary")
    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="darwin",
        run=run,
        launch_agent_path=launch_agent,
        app_version="1",
        topology_path=spec.topology_path,
    )
    assert manager.reconcile() == "enabled"
    assert manager.reconcile() == "enabled"
    assert sum(call[:2] == ["launchctl", "bootstrap"] for call in calls) == 1
    assert not any(call[:2] == ["launchctl", "kickstart"] for call in calls)


def test_failed_install_records_rollback_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "powershell.exe":
            return subprocess.CompletedProcess(command, 3, "", "")
        if command[0] == "whoami":
            return subprocess.CompletedProcess(
                command,
                0,
                '"DESKTOP\\User","S-1-5-21-100"\n',
                "",
            )
        return subprocess.CompletedProcess(command, 1, "", "failed")

    monkeypatch.setattr("node_service.require_durable_topology", lambda *args: "wss://primary")
    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="win32",
        run=run,
        app_version="1",
    )
    with pytest.raises(RuntimeError, match="failed"):
        manager.enable()
    assert read_status(spec.state_root)["transition"] == "rollback"


def test_frozen_artifact_and_windows_uninstaller_include_node_service() -> None:
    desktop = Path(__file__).resolve().parent
    spec = (desktop / "BetterAgent.spec").read_text(encoding="utf-8")
    installer = (desktop / "installer.iss").read_text(encoding="utf-8")
    assert '"node_service"' in spec
    assert '"node_source_launcher"' in spec
    assert "[UninstallRun]" in installer
    assert '--uninstall-node-service' in installer


def test_update_boots_out_owner_and_only_target_version_restores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    launch_agent = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    calls: list[list[str]] = []
    registered = False

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal registered
        calls.append(command)
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                command,
                0 if registered else 113,
                "",
                "" if registered else "Could not find service",
            )
        if command[:2] == ["launchctl", "bootstrap"]:
            registered = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["launchctl", "bootout"]:
            registered = False
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "failed")

    monkeypatch.setattr("node_service.require_durable_topology", lambda *args: "wss://primary")
    old = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="darwin",
        run=run,
        launch_agent_path=launch_agent,
        app_version="1",
    )
    assert old.reconcile() == "enabled"
    old.prepare_update("2")
    assert registered is False
    assert old.reconcile() == "updating"

    new = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="darwin",
        run=run,
        launch_agent_path=launch_agent,
        app_version="2",
    )
    assert new.reconcile() == "enabled"
    assert registered is True
    assert sum(call[:2] == ["launchctl", "bootout"] for call in calls) == 1
    old._release_update_lease()


def test_interrupted_update_restores_previous_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    launch_agent = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    registered = False

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal registered
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                command, 0 if registered else 113, "",
                "" if registered else "Could not find service",
            )
        if command[:2] == ["launchctl", "bootstrap"]:
            registered = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["launchctl", "bootout"]:
            registered = False
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "failed")

    monkeypatch.setattr("node_service.require_durable_topology", lambda *args: "wss://primary")
    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="darwin",
        run=run,
        launch_agent_path=launch_agent,
        app_version="1",
    )
    manager.reconcile()
    manager.prepare_update("2")
    manager._release_update_lease()
    assert manager.reconcile() == "enabled"
    assert registered is True


def test_uninstall_failure_is_not_recorded_as_applied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "powershell.exe":
            if "Export-ScheduledTask" in command[-1]:
                return subprocess.CompletedProcess(
                    command, 0, windows_task_xml(spec, "S-1-5-21-100"), "",
                )
            return subprocess.CompletedProcess(command, 0, "3\n", "")
        return subprocess.CompletedProcess(command, 1, "", "denied")

    monkeypatch.setattr("node_service.require_durable_topology", lambda *args: "wss://primary")
    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="win32",
        run=run,
        app_version="1",
    )
    with pytest.raises(RuntimeError, match="denied"):
        manager.remove()
    assert read_status(spec.state_root)["transition"] == "prepared"


@pytest.mark.parametrize(
    "old,new",
    (
        (
            "<RunLevel>LeastPrivilege</RunLevel>",
            "<RunLevel>HighestAvailable</RunLevel>",
        ),
        ("<Enabled>true</Enabled>", "<Enabled>false</Enabled>"),
    ),
)
def test_windows_projection_rejects_security_policy_drift(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    spec = _spec(tmp_path)
    drifted = windows_task_xml(spec, "S-1-5-21-100").replace(old, new)

    assert not _windows_projection_matches(drifted, spec, "S-1-5-21-100")


@pytest.mark.parametrize("platform", ("darwin", "win32"))
def test_remove_rejects_query_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    spec = _spec(tmp_path)
    command_name = "launchctl" if platform == "darwin" else "powershell.exe"

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        assert command[0] == command_name
        return subprocess.CompletedProcess(command, 5, "", "access denied")

    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform=platform,
        run=run,
        launch_agent_path=tmp_path / "agent.plist",
        app_version="1",
    )
    with pytest.raises(RuntimeError, match="access denied"):
        manager.remove()


def test_restart_clears_crash_budget_and_restarts_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    launch_agent = tmp_path / "agent.plist"
    launch_agent.parent.mkdir(exist_ok=True)
    launch_agent.write_bytes(plistlib.dumps(macos_plist(spec)))
    for attempt in range(5):
        assert begin_launcher_attempt(spec.state_root, now=100 + attempt)
    calls: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="darwin",
        run=run,
        launch_agent_path=launch_agent,
        app_version="1",
        topology_path=spec.topology_path,
    )
    manager.restart()
    assert any(command[:3] == ["launchctl", "kickstart", "-k"] for command in calls)
    assert begin_launcher_attempt(spec.state_root, now=106)


def test_reconcile_does_not_implicitly_clear_circuit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    launch_agent = tmp_path / "agent.plist"
    manager = NodeServiceManager(
        spec.executable,
        spec.state_root,
        platform="darwin",
        run=lambda command: subprocess.CompletedProcess(command, 0, "", ""),
        launch_agent_path=launch_agent,
        app_version="1",
        topology_path=spec.topology_path,
    )
    monkeypatch.setattr("node_service.require_durable_topology", lambda *args: "wss://primary")
    manager.reconcile()
    publish_launcher_status(spec.state_root, "circuit_open")
    for attempt in range(5):
        assert begin_launcher_attempt(spec.state_root, now=100 + attempt)
    manager.reconcile()
    assert not begin_launcher_attempt(spec.state_root, now=106)

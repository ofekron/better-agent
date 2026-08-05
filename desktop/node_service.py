from __future__ import annotations

import csv
import ipaddress
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from env_compat import dual_env, get_env
from i18n import t
from json_store import read_json, write_json_durable
from node_connection_status import read as read_connection_status
from node_launcher_lease import (
    NodeLauncherBusyError,
    NodeServiceManagementLease,
    NodeUpdateLease,
)
from paths import (
    ba_home,
    make_private_file,
    require_private_directory,
    require_private_file,
)
from topology import load_topology


LABEL = "com.betteragent.node"
WINDOWS_TASK = r"\Better Agent\Node"
_HEALTHY_RESET_SECONDS = 300.0
_CRASH_LIMIT = 5
_WINDOWS_RESTART_INTERVAL = "PT30S"


def _current_app_version() -> str:
    from _version import __version__

    return __version__


@dataclass(frozen=True)
class ServiceSpec:
    executable: Path
    state_root: Path
    topology_path: Path

    @property
    def command(self) -> tuple[str, ...]:
        return (
            str(self.executable),
            "--node-launcher",
            "--state-root",
            str(self.state_root),
            "--topology-path",
            str(self.topology_path),
        )


def _control_root(state_root: Path) -> Path:
    return state_root / "node-service"


def _desired_path(state_root: Path) -> Path:
    return _control_root(state_root) / "desired.json"


def _launcher_path(state_root: Path) -> Path:
    return _control_root(state_root) / "launcher.json"


def _crash_budget_path(state_root: Path) -> Path:
    return _control_root(state_root) / "crash-budget.json"


def _topology_attestation_path(state_root: Path) -> Path:
    return _control_root(state_root) / "topology-attestation.json"


def _safe_root(raw: Path) -> Path:
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError("node service state root must be absolute")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private_directory(root)
    return root.resolve(strict=True)


def _safe_executable(raw: Path) -> Path:
    executable = Path(raw).expanduser().resolve(strict=True)
    if not executable.is_file():
        raise ValueError("node service executable must be a file")
    return executable


def _safe_topology_path(raw: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("node topology path must be absolute")
    return path.resolve(strict=False)


def _validate_topology_file(path: Path) -> None:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved != path:
        raise ValueError("node topology path must be a file")
    require_private_file(path)


def attest_durable_topology(address: str, state_root: Path | None = None) -> None:
    root = _safe_root(state_root or ba_home())
    _write_private(
        _topology_attestation_path(root),
        {
            "schema_version": 1,
            "address": address,
            "verified_at": time.time(),
        },
    )


def require_durable_topology(state_root: Path | None = None) -> str:
    address = load_topology().primary.address.strip()
    parsed = urlsplit(address)
    if parsed.scheme.lower() != "wss" or not parsed.hostname:
        raise RuntimeError(t("desktop.node.wss_required"))
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if loopback or hostname.endswith(".localhost"):
        raise RuntimeError(t("desktop.node.wss_required"))
    root = _safe_root(state_root or ba_home())
    attestation = read_json(_topology_attestation_path(root), {})
    if (
        attestation.get("schema_version") != 1
        or attestation.get("address") != address
    ):
        raise RuntimeError(t("desktop.node.wss_attestation_required"))
    return address


def _write_private(path: Path, value: dict[str, object]) -> None:
    write_json_durable(path, value)
    make_private_file(path)


def publish_launcher_status(
    state_root: Path,
    state: str,
    *,
    detail: str = "",
) -> dict[str, object]:
    if state not in {"starting", "running", "failed", "circuit_open", "stopped"}:
        raise ValueError(f"invalid node launcher state: {state}")
    value: dict[str, object] = {
        "schema_version": 1,
        "state": state,
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    if detail:
        value["detail"] = detail[:160]
    _write_private(_launcher_path(_safe_root(state_root)), value)
    return value


def read_status(state_root: Path | None = None) -> dict[str, object]:
    root = _safe_root(state_root or ba_home())
    desired = read_json(_desired_path(root), {})
    launcher = read_json(_launcher_path(root), {})
    connection = read_connection_status(root)
    return {
        "desired": desired.get("desired_state", "disabled"),
        "transition": desired.get("transition", "none"),
        "launcher": launcher.get("state", "stopped"),
        "connection": connection.get("state", "unreachable"),
    }


def begin_launcher_attempt(
    state_root: Path,
    *,
    now: float | None = None,
) -> bool:
    root = _safe_root(state_root)
    observed = time.time() if now is None else now
    with NodeServiceManagementLease.acquire(root, checkout=Path.cwd()):
        path = _crash_budget_path(root)
        current = read_json(path, {})
        failure_count = current.get("failure_count")
        if not isinstance(failure_count, int) or failure_count < 0:
            legacy_attempts = current.get("attempts")
            if not current:
                failure_count = 0
            elif isinstance(legacy_attempts, list):
                failure_count = len(legacy_attempts)
            else:
                failure_count = _CRASH_LIMIT
        healthy_since = current.get("healthy_since")
        if (
            isinstance(healthy_since, (int, float))
            and observed - float(healthy_since) >= _HEALTHY_RESET_SECONDS
        ):
            failure_count = 0
        if failure_count >= _CRASH_LIMIT:
            return False
        _write_private(
            path,
            {"schema_version": 2, "failure_count": failure_count + 1},
        )
        return True


def mark_launcher_healthy(
    state_root: Path,
    *,
    now: float | None = None,
) -> None:
    root = _safe_root(state_root)
    observed = time.time() if now is None else now
    with NodeServiceManagementLease.acquire(root, checkout=Path.cwd()):
        path = _crash_budget_path(root)
        current = read_json(path, {})
        failure_count = current.get("failure_count", 0)
        if not isinstance(failure_count, int) or failure_count < 0:
            failure_count = 0
        _write_private(
            path,
            {
                "schema_version": 2,
                "failure_count": failure_count,
                "healthy_since": observed,
            },
        )


def macos_plist(spec: ServiceSpec) -> dict[str, object]:
    log_path = spec.state_root / "node-service.log"
    return {
        "Label": LABEL,
        "ProgramArguments": list(spec.command),
        "EnvironmentVariables": {
            "BETTER_AGENT_HOME": str(spec.state_root),
            "BETTER_CLAUDE_HOME": str(spec.state_root),
            "BETTER_AGENT_TOPOLOGY_PATH": str(spec.topology_path),
            "BETTER_CLAUDE_TOPOLOGY_PATH": str(spec.topology_path),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def windows_task_xml(spec: ServiceSpec, user_sid: str) -> str:
    if not user_sid.startswith("S-") or any(ch.isspace() for ch in user_sid):
        raise ValueError("invalid Windows user SID")
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    task = ET.Element(f"{{{ns}}}Task", {"version": "1.4"})
    triggers = ET.SubElement(task, f"{{{ns}}}Triggers")
    trigger = ET.SubElement(triggers, f"{{{ns}}}LogonTrigger")
    ET.SubElement(trigger, f"{{{ns}}}Enabled").text = "true"
    ET.SubElement(trigger, f"{{{ns}}}UserId").text = user_sid
    principals = ET.SubElement(task, f"{{{ns}}}Principals")
    principal = ET.SubElement(principals, f"{{{ns}}}Principal", {"id": "Author"})
    ET.SubElement(principal, f"{{{ns}}}UserId").text = user_sid
    ET.SubElement(principal, f"{{{ns}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{ns}}}RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, f"{{{ns}}}Settings")
    ET.SubElement(settings, f"{{{ns}}}MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, f"{{{ns}}}DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{ns}}}StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{ns}}}StartWhenAvailable").text = "true"
    ET.SubElement(settings, f"{{{ns}}}Enabled").text = "true"
    ET.SubElement(settings, f"{{{ns}}}ExecutionTimeLimit").text = "PT0S"
    restart = ET.SubElement(settings, f"{{{ns}}}RestartOnFailure")
    ET.SubElement(restart, f"{{{ns}}}Interval").text = _WINDOWS_RESTART_INTERVAL
    ET.SubElement(restart, f"{{{ns}}}Count").text = str(_CRASH_LIMIT)
    actions = ET.SubElement(task, f"{{{ns}}}Actions", {"Context": "Author"})
    execute = ET.SubElement(actions, f"{{{ns}}}Exec")
    ET.SubElement(execute, f"{{{ns}}}Command").text = str(spec.executable)
    ET.SubElement(execute, f"{{{ns}}}Arguments").text = subprocess.list2cmdline(
        list(spec.command[1:])
    )
    return ET.tostring(task, encoding="unicode")


def _windows_projection_matches(
    xml: str,
    spec: ServiceSpec,
    user_sid: str,
) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    expected = {
        ".//{*}LogonTrigger/{*}Enabled": "true",
        ".//{*}LogonTrigger/{*}UserId": user_sid,
        ".//{*}Principal/{*}UserId": user_sid,
        ".//{*}Principal/{*}LogonType": "InteractiveToken",
        ".//{*}Principal/{*}RunLevel": "LeastPrivilege",
        ".//{*}MultipleInstancesPolicy": "IgnoreNew",
        ".//{*}DisallowStartIfOnBatteries": "false",
        ".//{*}StopIfGoingOnBatteries": "false",
        ".//{*}StartWhenAvailable": "true",
        ".//{*}Settings/{*}Enabled": "true",
        ".//{*}ExecutionTimeLimit": "PT0S",
        ".//{*}RestartOnFailure/{*}Interval": _WINDOWS_RESTART_INTERVAL,
        ".//{*}RestartOnFailure/{*}Count": str(_CRASH_LIMIT),
        ".//{*}Exec/{*}Command": str(spec.executable),
        ".//{*}Exec/{*}Arguments": subprocess.list2cmdline(
            list(spec.command[1:])
        ),
    }
    if not all(
        len(root.findall(path)) == 1 and root.findtext(path) == value
        for path, value in expected.items()
    ):
        return False
    return (
        len(root.findall(".//{*}Triggers/*")) == 1
        and len(root.findall(".//{*}LogonTrigger")) == 1
        and len(root.findall(".//{*}Principal")) == 1
        and len(root.findall(".//{*}Actions/*")) == 1
        and len(root.findall(".//{*}Exec")) == 1
        and root.find(".//{*}Principal").get("id") == "Author"
        and root.find(".//{*}Actions").get("Context") == "Author"
    )


def _mac_service_missing(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode == 113 or "could not find service" in output


def _windows_task_missing(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 3


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def _windows_user_sid(run: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> str:
    result = run(["whoami", "/user", "/fo", "csv", "/nh"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not resolve Windows user SID")
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) < 2:
        raise RuntimeError("Windows user SID response is invalid")
    sid = rows[0][1].strip()
    if not sid.startswith("S-"):
        raise RuntimeError("Windows user SID response is invalid")
    return sid


def _mac_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


class NodeServiceManager:
    def __init__(
        self,
        executable: Path | None = None,
        state_root: Path | None = None,
        *,
        platform: str | None = None,
        run: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
        launch_agent_path: Path | None = None,
        app_version: str | None = None,
        topology_path: Path | None = None,
    ) -> None:
        root = _safe_root(state_root or ba_home())
        desired = read_json(_desired_path(root), {})
        configured_topology = topology_path or Path(
            get_env("BETTER_CLAUDE_TOPOLOGY_PATH")
            or desired.get("topology_path")
            or root / "topology.yaml"
        )
        resolved_topology = _safe_topology_path(configured_topology)
        os.environ.update(
            dual_env("BETTER_CLAUDE_TOPOLOGY_PATH", str(resolved_topology))
        )
        self.spec = ServiceSpec(
            _safe_executable(executable or Path(sys.executable)),
            root,
            resolved_topology,
        )
        self.platform = platform or sys.platform
        self._run = run
        self._launch_agent_path = launch_agent_path or _mac_path()
        self._app_version = app_version or _current_app_version()
        self._update_lease: NodeUpdateLease | None = None

    def enable(self) -> None:
        with NodeServiceManagementLease.acquire(
            self.spec.state_root,
            checkout=self.spec.executable.parent,
        ):
            self._enable_locked()

    def remove(self) -> None:
        with NodeServiceManagementLease.acquire(
            self.spec.state_root,
            checkout=self.spec.executable.parent,
        ):
            self._write_desired("removed", "prepared")
            self._remove_projection_checked()
            self._write_desired("removed", "applied")

    def reconcile(self) -> str:
        with NodeServiceManagementLease.acquire(
            self.spec.state_root,
            checkout=self.spec.executable.parent,
        ):
            desired = read_json(_desired_path(self.spec.state_root), {})
            if desired.get("desired_state") == "removed":
                self._remove_projection_checked()
                if desired.get("transition") != "applied":
                    self._write_desired("removed", "applied")
                return "removed"
            if desired.get("desired_state") == "updating":
                if desired.get("target_version") == self._app_version:
                    self._enable_locked()
                    return "enabled"
                if self._update_is_owned():
                    return "updating"
                self._enable_locked()
                return "enabled"
            if self._desired_matches(desired) and self._projection_present():
                self._start_projection()
                return "enabled"
            self._enable_locked()
            return "enabled"

    def prepare_update(self, target_version: str) -> bool:
        target = target_version.strip()
        if not target:
            raise ValueError("target version is required")
        with NodeServiceManagementLease.acquire(
            self.spec.state_root,
            checkout=self.spec.executable.parent,
        ):
            desired = read_json(_desired_path(self.spec.state_root), {})
            if desired.get("desired_state") == "removed":
                self._remove_projection_checked()
                return False
            if desired.get("desired_state") == "updating":
                raise RuntimeError("node service update is already in progress")
            update_lease = NodeUpdateLease.acquire(
                self.spec.state_root,
                checkout=self.spec.executable.parent,
            )
            self._write_desired(
                "updating",
                "prepared",
                target_version=target,
            )
            try:
                self._remove_projection_checked()
            except Exception:
                update_lease.release()
                self._enable_locked()
                raise
            self._update_lease = update_lease
            return True

    def restore_after_failed_update(self) -> None:
        with NodeServiceManagementLease.acquire(
            self.spec.state_root,
            checkout=self.spec.executable.parent,
        ):
            self._release_update_lease()
            self._enable_locked()

    def restart(self) -> None:
        with NodeServiceManagementLease.acquire(
            self.spec.state_root,
            checkout=self.spec.executable.parent,
        ):
            if not self._projection_present():
                raise RuntimeError("node service is not installed")
            self._restart_projection_locked()

    def status(self) -> dict[str, object]:
        return read_status(self.spec.state_root)

    def _start_projection(self) -> None:
        if self.platform == "darwin":
            domain = f"gui/{os.getuid()}"
            result = self._run(["launchctl", "print", f"{domain}/{LABEL}"])
        elif self.platform.startswith("win"):
            result = self._run(["schtasks", "/Run", "/TN", WINDOWS_TASK])
        else:
            raise RuntimeError("durable node service requires macOS or Windows")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "node service start failed")

    def _write_desired(
        self,
        desired_state: str,
        transition: str,
        *,
        target_version: str = "",
    ) -> None:
        value: dict[str, object] = {
            "schema_version": 1,
            "desired_state": desired_state,
            "transition": transition,
            "executable": str(self.spec.executable),
            "state_root": str(self.spec.state_root),
            "topology_path": str(self.spec.topology_path),
            "platform": self.platform,
            "app_version": self._app_version,
            "updated_at": time.time(),
        }
        if target_version:
            value["target_version"] = target_version
        _write_private(
            _desired_path(self.spec.state_root),
            value,
        )

    def _enable_locked(self) -> None:
        _validate_topology_file(self.spec.topology_path)
        require_durable_topology(self.spec.state_root)
        self._write_desired("enabled", "prepared")
        try:
            self._install_projection()
            self._write_desired("enabled", "applied")
            if self.platform.startswith("win"):
                self._start_projection()
        except Exception:
            self._write_desired("enabled", "rollback")
            raise

    def _desired_matches(self, desired: dict[str, object]) -> bool:
        return (
            desired.get("desired_state") == "enabled"
            and desired.get("transition") == "applied"
            and desired.get("executable") == str(self.spec.executable)
            and desired.get("state_root") == str(self.spec.state_root)
            and desired.get("topology_path") == str(self.spec.topology_path)
            and desired.get("platform") == self.platform
            and desired.get("app_version") == self._app_version
        )

    def _projection_present(self) -> bool:
        if self.platform == "darwin":
            try:
                matches = plistlib.loads(
                    self._launch_agent_path.read_bytes()
                ) == macos_plist(
                    self.spec
                )
            except (OSError, ValueError, plistlib.InvalidFileException):
                return False
            domain = f"gui/{os.getuid()}"
            registered = self._run(["launchctl", "print", f"{domain}/{LABEL}"])
            if registered.returncode == 0:
                return matches
            if _mac_service_missing(registered):
                return False
            raise RuntimeError(
                registered.stderr.strip() or "node service query failed"
            )
        if self.platform.startswith("win"):
            result = self._query_windows_task_xml()
            if result.returncode != 0:
                if _windows_task_missing(result):
                    return False
                raise RuntimeError(
                    result.stderr.strip() or "node service query failed"
                )
            return _windows_projection_matches(
                result.stdout, self.spec, _windows_user_sid(self._run)
            )
        raise RuntimeError("durable node service requires macOS or Windows")

    def _install_projection(self) -> None:
        if self.platform == "darwin":
            path = self._launch_agent_path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            domain = f"gui/{os.getuid()}"
            registered = self._run(["launchctl", "print", f"{domain}/{LABEL}"])
            if registered.returncode == 0:
                stopped = self._run([
                    "launchctl", "bootout", f"{domain}/{LABEL}",
                ])
                if stopped.returncode != 0:
                    raise RuntimeError(
                        stopped.stderr.strip() or "node service bootout failed"
                    )
            elif not _mac_service_missing(registered):
                raise RuntimeError(
                    registered.stderr.strip() or "node service query failed"
                )
            _atomic_bytes(path, plistlib.dumps(macos_plist(self.spec)))
            result = self._run(["launchctl", "bootstrap", domain, str(path)])
        elif self.platform.startswith("win"):
            self._remove_projection_checked()
            sid = _windows_user_sid(self._run)
            xml = windows_task_xml(self.spec, sid)
            control_root = _control_root(self.spec.state_root)
            control_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            require_private_directory(control_root)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".task.",
                suffix=".xml",
                dir=control_root,
                text=True,
            )
            xml_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(xml)
                    handle.flush()
                    os.fsync(handle.fileno())
                make_private_file(xml_path)
                result = self._run([
                    "schtasks", "/Create", "/TN", WINDOWS_TASK,
                    "/XML", str(xml_path), "/F",
                ])
            finally:
                xml_path.unlink(missing_ok=True)
        else:
            raise RuntimeError("durable node service requires macOS or Windows")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "node service install failed")

    def _remove_projection_checked(self) -> None:
        if self.platform == "darwin":
            domain = f"gui/{os.getuid()}"
            registered = self._run(["launchctl", "print", f"{domain}/{LABEL}"])
            if registered.returncode == 0:
                result = self._run(["launchctl", "bootout", f"{domain}/{LABEL}"])
                if result.returncode != 0:
                    raise RuntimeError(
                        result.stderr.strip() or "node service bootout failed"
                    )
            elif not _mac_service_missing(registered):
                raise RuntimeError(
                    registered.stderr.strip() or "node service query failed"
                )
            self._launch_agent_path.unlink(missing_ok=True)
        elif self.platform.startswith("win"):
            query = self._query_windows_task_xml()
            if query.returncode == 0:
                if self._windows_task_is_running():
                    ended = self._run(["schtasks", "/End", "/TN", WINDOWS_TASK])
                    if ended.returncode != 0:
                        raise RuntimeError(
                            ended.stderr.strip() or "node service stop failed"
                        )
                deleted = self._run([
                    "schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F",
                ])
                if deleted.returncode != 0:
                    raise RuntimeError(
                        deleted.stderr.strip() or "node service removal failed"
                    )
            elif not _windows_task_missing(query):
                raise RuntimeError(
                    query.stderr.strip() or "node service query failed"
                )
        else:
            raise RuntimeError("durable node service requires macOS or Windows")

    def _windows_task_is_running(self) -> bool:
        script = (
            "$task=Get-ScheduledTask -TaskPath '\\Better Agent\\' "
            "-TaskName 'Node' -ErrorAction Stop; [int]$task.State"
        )
        result = self._run([
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", script,
        ])
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "node service state query failed"
            )
        try:
            return int(result.stdout.strip()) == 4
        except ValueError as exc:
            raise RuntimeError("node service state response is invalid") from exc

    def _restart_projection_locked(self) -> None:
        _crash_budget_path(self.spec.state_root).unlink(missing_ok=True)
        if self.platform == "darwin":
            domain = f"gui/{os.getuid()}"
            result = self._run([
                "launchctl", "kickstart", "-k", f"{domain}/{LABEL}",
            ])
        elif self.platform.startswith("win"):
            if self._windows_task_is_running():
                stopped = self._run(["schtasks", "/End", "/TN", WINDOWS_TASK])
                if stopped.returncode != 0:
                    raise RuntimeError(
                        stopped.stderr.strip() or "node service stop failed"
                    )
            result = self._run(["schtasks", "/Run", "/TN", WINDOWS_TASK])
        else:
            raise RuntimeError("durable node service requires macOS or Windows")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "node service restart failed")

    def _query_windows_task_xml(self) -> subprocess.CompletedProcess[str]:
        script = (
            "$ErrorActionPreference='Stop';try{"
            "$null=Get-ScheduledTask -TaskPath '\\Better Agent\\' "
            "-TaskName 'Node';Export-ScheduledTask "
            "-TaskPath '\\Better Agent\\' -TaskName 'Node'"
            "}catch [Microsoft.Management.Infrastructure.CimException]{"
            "if([int]$_.Exception.StatusCode -eq 6){exit 3};throw}"
        )
        return self._run([
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", script,
        ])

    def _update_is_owned(self) -> bool:
        try:
            lease = NodeUpdateLease.acquire(
                self.spec.state_root,
                checkout=self.spec.executable.parent,
            )
        except NodeLauncherBusyError:
            return True
        lease.release()
        return False

    def _release_update_lease(self) -> None:
        if self._update_lease is None:
            return
        self._update_lease.release()
        self._update_lease = None


def _atomic_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        make_private_file(path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

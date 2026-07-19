from __future__ import annotations

import importlib.util
import json
import plistlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "run_service.py"
spec = importlib.util.spec_from_file_location("run_service", MODULE_PATH)
run_service = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = run_service
spec.loader.exec_module(run_service)


def test_launch_agent_restarts_current_checkout() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        (checkout / "run.sh").write_text("#!/bin/bash\n")
        home = root / "state"
        target = run_service.ServiceTarget(
            checkout, home, ("/bin/bash", str(checkout / "run.sh"), "--service-child"), (),
        )
        payload = run_service.launch_agent(target)
        assert payload["RunAtLoad"] is True
        assert payload["KeepAlive"] is True
        assert payload["WorkingDirectory"] == str(checkout)
        assert payload["ProgramArguments"] == ["/bin/bash", str(checkout / "run.sh"), "--service-child"]
        assert plistlib.loads(plistlib.dumps(payload)) == payload


def test_systemd_restarts_current_checkout() -> None:
    checkout = Path("/tmp/Better Agent checkout")
    home = Path("/tmp/Better Agent state")
    target = run_service.ServiceTarget(
        checkout, home, ("/bin/bash", str(checkout / "run.sh"), "--service-child"), (),
    )
    unit = run_service.systemd_unit(target)
    assert f'ExecStart="/bin/bash" "{checkout / "run.sh"}" "--service-child"' in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit


def test_service_prefers_matching_bas_line() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        (checkout / "run.sh").write_text("#!/bin/bash\n")
        bas_home = root / "line-home"
        config = {"lines": {"dev": {"checkout": str(checkout), "home": str(bas_home), "port": 28941}}}
        completed = run_service.subprocess.CompletedProcess([], 0, json.dumps(config), "")
        with patch.dict(run_service.os.environ, {"BA_SWITCH_HOME": str(root / "bas-state")}), patch.object(
            run_service, "_bas_executable", return_value="/opt/bin/bas",
        ), patch.object(run_service.subprocess, "run", return_value=completed):
            target = run_service.resolve_target(checkout.resolve(), root / "fallback-home")
        assert target.command == ("/opt/bin/bas", "exec-line", "dev")
        assert target.home == bas_home.resolve()
        assert dict(target.environment)["BAS_NO_SELF_UPDATE"] == "1"
        assert dict(target.environment)["BA_SWITCH_HOME"] == str(root / "bas-state")


def test_service_falls_back_when_bas_does_not_own_checkout() -> None:
    checkout = Path("/tmp/current-checkout")
    home = Path("/tmp/current-home")
    with patch.object(run_service, "_bas_executable", return_value=""):
        target = run_service.resolve_target(checkout, home)
    assert target.command == ("/bin/bash", str(checkout / "run.sh"), "--service-child")


def test_run_sh_exposes_reversible_service_commands() -> None:
    source = (ROOT / "run.sh").read_text()
    assert "--install-service|--uninstall-service|--service-status" in source
    assert "BETTER_AGENT_RUN_SH_SERVICE_CHILD" in source
    assert source.index("--install-service|--uninstall-service|--service-status") < source.index("bas_available()")


if __name__ == "__main__":
    test_launch_agent_restarts_current_checkout()
    test_systemd_restarts_current_checkout()
    test_service_prefers_matching_bas_line()
    test_service_falls_back_when_bas_does_not_own_checkout()
    test_run_sh_exposes_reversible_service_commands()

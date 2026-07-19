from __future__ import annotations

import importlib.util
import plistlib
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "run_service.py"
spec = importlib.util.spec_from_file_location("run_service", MODULE_PATH)
run_service = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(run_service)


def test_launch_agent_restarts_current_checkout() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        (checkout / "run.sh").write_text("#!/bin/bash\n")
        home = root / "state"
        payload = run_service.launch_agent(checkout, home)
        assert payload["RunAtLoad"] is True
        assert payload["KeepAlive"] is True
        assert payload["WorkingDirectory"] == str(checkout)
        assert payload["ProgramArguments"] == ["/bin/bash", str(checkout / "run.sh"), "--service-child"]
        assert plistlib.loads(plistlib.dumps(payload)) == payload


def test_systemd_restarts_current_checkout() -> None:
    checkout = Path("/tmp/Better Agent checkout")
    home = Path("/tmp/Better Agent state")
    unit = run_service.systemd_unit(checkout, home)
    assert f'ExecStart=/bin/bash "{checkout / "run.sh"}" --service-child' in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit


def test_run_sh_exposes_reversible_service_commands() -> None:
    source = (ROOT / "run.sh").read_text()
    assert "--install-service|--uninstall-service|--service-status" in source
    assert "BETTER_AGENT_RUN_SH_SERVICE_CHILD" in source
    assert source.index("--install-service|--uninstall-service|--service-status") < source.index("bas_available()")


if __name__ == "__main__":
    test_launch_agent_restarts_current_checkout()
    test_systemd_restarts_current_checkout()
    test_run_sh_exposes_reversible_service_commands()

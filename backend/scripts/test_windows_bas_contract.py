from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proc_control


def test_provider_breakaway_is_limited_to_bas_service_children() -> None:
    control = proc_control._WindowsProcessControl()
    with patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True):
        with patch.dict(os.environ, {}, clear=True):
            ordinary = control.detach_spawn_kwargs()["creationflags"]
        with patch.dict(
            os.environ,
            {"BETTER_AGENT_RUN_SH_SERVICE_CHILD": "1"},
            clear=True,
        ):
            supervised = control.detach_spawn_kwargs()["creationflags"]
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    assert ordinary & breakaway == 0
    assert supervised & breakaway == breakaway


def test_windows_launcher_honors_bas_service_contract() -> None:
    source = (Path(__file__).resolve().parents[2] / "run_windows.bat").read_text()
    assert 'if /I "%~1"=="--service-child"' in source
    assert 'if not defined BETTER_AGENT_BACKEND_PORT' in source
    assert '--port %BETTER_AGENT_BACKEND_PORT%' in source
    assert 'if "%SERVICE_CHILD%"=="0"' in source


if __name__ == "__main__":
    test_provider_breakaway_is_limited_to_bas_service_children()
    test_windows_launcher_honors_bas_service_contract()
    print("windows BAS contract tests passed")

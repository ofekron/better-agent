#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from execution_bypass_audit import (  # noqa: E402
    ALLOWED_SURFACES,
    ExecutionSurface,
    audit_backend,
    inventory_source,
)


def test_production_inventory_is_fully_owned() -> None:
    assert audit_backend(BACKEND) == ALLOWED_SURFACES


def test_representative_bypasses_are_detected() -> None:
    cases = (
        (
            "provider_codex.py",
            "def bypass():\n"
            "    return resolve_cli_binary('codex')\n",
            "discovery",
        ),
        (
            "runner_codex.py",
            "from asyncio import create_subprocess_exec as spawn\n"
            "async def bypass():\n"
            "    return await spawn('codex')\n",
            "process",
        ),
        (
            "run_recovery.py",
            "def bypass():\n"
            "    return config_store.get_provider('provider-id')\n",
            "static_authority",
        ),
    )
    for module, source, category in cases:
        observed = inventory_source(source, module)
        assert len(observed) == 1
        surface = next(iter(observed))
        assert surface.category == category
        assert surface not in ALLOWED_SURFACES


def test_public_provider_start_run_override_is_detected() -> None:
    observed = inventory_source(
        "class FuguProvider:\n"
        "    def start_run(self):\n"
        "        return None\n",
        "provider_fugu.py",
    )
    assert ExecutionSurface(
        "public_start_run",
        "provider_fugu.py",
        "FuguProvider.start_run",
        "start_run",
    ) in observed


def test_real_process_trace_records_only_explicit_argv() -> None:
    probe = (
        "import json,sys\n"
        "events=[]\n"
        "sys.addaudithook(lambda event,args: "
        "events.append([event,args[0],list(args[1])]) "
        "if event == 'subprocess.Popen' else None)\n"
        "import subprocess\n"
        "subprocess.run([sys.executable,'-c','pass'],check=True)\n"
        "print(json.dumps(events))\n"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR"}
    }
    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    events = json.loads(completed.stdout)
    assert events == [[
        "subprocess.Popen",
        sys.executable,
        [sys.executable, "-c", "pass"],
    ]]


TESTS = (
    test_production_inventory_is_fully_owned,
    test_representative_bypasses_are_detected,
    test_public_provider_start_run_override_is_detected,
    test_real_process_trace_records_only_explicit_argv,
)


if __name__ == "__main__":
    temp_home = tempfile.mkdtemp(prefix="execution-bypass-audit-")
    os.environ["BETTER_AGENT_HOME"] = temp_home
    try:
        for test in TESTS:
            test()
    finally:
        import shutil
        shutil.rmtree(temp_home)
    print("PASS execution bypass audit")

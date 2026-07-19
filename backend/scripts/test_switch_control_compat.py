"""Switch-control compatibility and extension/core boundary regression."""

from __future__ import annotations

import shutil
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from daemonhost import switch_control

MANIFEST = REPO / "extensions" / "switch-control" / "better-agent-extension.json"

manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
assert manifest["surfaces"] == ["instructions", "daemons"], manifest
assert "backend_module" not in manifest["entrypoints"], manifest
assert manifest["permissions"] == {"daemons": "supervisor"}, manifest
assert "access-url" not in (REPO / "daemonhost" / "switch_control.py").read_text(encoding="utf-8")

assert switch_control._incompatible(str(REPO)) == []

tmp = tempfile.mkdtemp(prefix="ba-compat-")
try:
    cold = Path(tmp) / "cold"
    cold.mkdir()
    missing = switch_control._incompatible(str(cold))
    assert missing == list(switch_control._REQUIRED_CHECKOUT_FILES), missing

    warm = Path(tmp) / "warm"
    for relative in switch_control._REQUIRED_CHECKOUT_FILES:
        path = warm / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    assert switch_control._incompatible(str(warm)) == []
finally:
    shutil.rmtree(tmp)

projection_home = tempfile.mkdtemp(prefix="ba-switch-lines-")
prior_home = os.environ.get("BETTER_AGENT_HOME")
try:
    os.environ["BETTER_AGENT_HOME"] = projection_home
    base = Path(projection_home) / "better-agent"
    dev = base
    qa = Path(f"{base}-qa")
    main = Path(f"{base}-main")
    for checkout in (dev, qa, main):
        (checkout / "backend" / ".venv" / "bin").mkdir(parents=True)
        (checkout / "backend" / "main.py").write_text("", encoding="utf-8")
        (checkout / "backend" / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        for relative in switch_control._REQUIRED_CHECKOUT_FILES:
            path = checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    (Path(projection_home) / "switch_lines.json").write_text(
        json.dumps(
            {
                "dev": {"checkout": str(dev), "backend_port": 18765},
                "qa": {"checkout": str(qa), "backend_port": 18767},
                "main": {"checkout": str(main), "backend_port": 18766},
            }
        ),
        encoding="utf-8",
    )
    discovered = switch_control.state(str(main))
    assert discovered["lines"] == {
        "dev": str(dev.resolve()),
        "qa": str(qa.resolve()),
        "main": str(main.resolve()),
    }, discovered
    assert discovered["line_targets"]["dev"]["backend_port"] == 18765
    parallel_request = switch_control.reserve(str(main), "dev", "parallel-dev")
    assert parallel_request["status"] == "succeeded"
    assert parallel_request["target_url"] == "http://127.0.0.1:18765"
    assert discovered["active_line"] == "main" and discovered["switchable"] is True
finally:
    if prior_home is None:
        os.environ.pop("BETTER_AGENT_HOME", None)
    else:
        os.environ["BETTER_AGENT_HOME"] = prior_home
    shutil.rmtree(projection_home)

print("OK test_switch_control_compat")

"""Switch-control compatibility and extension/core boundary regression."""

from __future__ import annotations

import shutil
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from daemonhost import switch_control

ROUTES = REPO / "extensions" / "switch-control" / "backend" / "routes.py"

source = ROUTES.read_text(encoding="utf-8")
for forbidden in ("daemonhost", "sys.path", "active_checkout", "call_internal"):
    assert forbidden not in source, f"extension route crosses core boundary via {forbidden}"
assert 'invoke_capability("switch-control", "state.get")' in source
assert '"switch.request"' in source

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
prior_parallel = os.environ.get("BETTER_AGENT_PARALLEL_LINES")
try:
    os.environ["BETTER_AGENT_HOME"] = projection_home
    os.environ["BETTER_AGENT_PARALLEL_LINES"] = "1"
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
        '{"main": "' + str(main) + '"}', encoding="utf-8"
    )
    discovered = switch_control.state(str(main))
    assert discovered["lines"] == {
        "dev": str(dev.resolve()),
        "qa": str(qa.resolve()),
        "main": str(main.resolve()),
    }, discovered
    assert discovered["line_targets"]["dev"]["home"].endswith(".better-claude")
    assert discovered["line_targets"]["dev"]["backend_port"] == 18765
    assert discovered["line_targets"]["main"]["home"].endswith(".better-claude-main")
    assert discovered["line_targets"]["main"]["backend_port"] == 18766
    assert discovered["line_targets"]["qa"]["home"].endswith(".better-claude-qa")
    assert discovered["line_targets"]["qa"]["backend_port"] == 18767
    assert discovered["active_line"] == "main" and discovered["switchable"] is True
    parallel_request = switch_control.reserve(str(main), "dev", "parallel-dev")
    assert parallel_request["status"] == "succeeded"
    assert parallel_request["target_url"] == "http://127.0.0.1:18765"
finally:
    if prior_home is None:
        os.environ.pop("BETTER_AGENT_HOME", None)
    else:
        os.environ["BETTER_AGENT_HOME"] = prior_home
    if prior_parallel is None:
        os.environ.pop("BETTER_AGENT_PARALLEL_LINES", None)
    else:
        os.environ["BETTER_AGENT_PARALLEL_LINES"] = prior_parallel
    shutil.rmtree(projection_home)

print("OK test_switch_control_compat")

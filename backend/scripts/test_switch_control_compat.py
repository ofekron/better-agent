"""Switch-control compatibility and extension/core boundary regression."""

from __future__ import annotations

import shutil
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

print("OK test_switch_control_compat")

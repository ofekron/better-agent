"""Locks the switch-control incompatibility gate: a line missing the daemonhost
modules this extension imports must be reported incompatible, so /state can
disable it and /switch can refuse it — otherwise switching there 500s the
switcher and strands the user with no way back (the main-line failure this
fixes).

Loads the real routes.py by file path (as the extension backend host does),
stubbing the host-injected SDK so it imports outside the host.

Run: backend/.venv/bin/python backend/scripts/test_switch_control_compat.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# The extension backend host injects better_agent_sdk; stub it so routes.py's
# module-level `from better_agent_sdk import Client` resolves in isolation.
_sdk = types.ModuleType("better_agent_sdk")
_sdk.Client = object
sys.modules.setdefault("better_agent_sdk", _sdk)

os.environ["BETTER_AGENT_ACTIVE_CHECKOUT"] = str(REPO)

routes_path = REPO / "extensions" / "switch-control" / "backend" / "routes.py"
spec = importlib.util.spec_from_file_location("switch_control_routes", routes_path)
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)

# The running dev checkout has daemonhost -> compatible.
assert routes._missing_checkout_modules(str(REPO)) == [], "the dev checkout must be compatible"

tmp = tempfile.mkdtemp(prefix="ba-compat-")
try:
    # A checkout lacking daemonhost (the main line, 1428 commits behind) is
    # incompatible and names every required module it lacks.
    cold = Path(tmp) / "co"
    (cold / "backend").mkdir(parents=True)
    missing = routes._missing_checkout_modules(str(cold))
    assert missing == list(routes._REQUIRED_CHECKOUT_MODULES), missing
    assert "daemonhost/pointer.py" in missing

    # A checkout with daemonhost present is compatible.
    warm = Path(tmp) / "warm"
    (warm / "daemonhost").mkdir(parents=True)
    for rel in routes._REQUIRED_CHECKOUT_MODULES:
        p = warm / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    assert routes._missing_checkout_modules(str(warm)) == [], "a checkout with daemonhost is compatible"
finally:
    shutil.rmtree(tmp)

print("OK test_switch_control_compat")

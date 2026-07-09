from __future__ import annotations

import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-remote-spawn-")
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="bc-test-remote-spawn-os-home-"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_remote  # noqa: E402


def test_start_run_binds_extension_policy_records_locally() -> None:
    """Regression: `start_run` referenced `session_record`/`worker_record`
    when building the spawn_run payload but never assigned them, so every
    remote spawn raised NameError at payload construction. They must be
    local names of the function, not free/global lookups."""
    code = provider_remote.RemoteProviderProxy.start_run.__code__
    for name in ("session_record", "worker_record"):
        if name in code.co_names and name not in code.co_varnames:
            raise AssertionError(
                f"{name!r} is referenced as a free/global name in start_run; "
                f"it must be assigned locally before the spawn_run payload"
            )
        if name not in code.co_varnames:
            raise AssertionError(f"{name!r} is not bound in start_run")


if __name__ == "__main__":
    test_start_run_binds_extension_policy_records_locally()
    print("provider_remote spawn-payload test passed")

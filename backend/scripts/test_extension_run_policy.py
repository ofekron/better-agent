from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-extension-run-policy-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import config_store  # noqa: E402
import session_store  # noqa: E402
from extension_run_policy import disabled_builtin_extensions_for_run  # noqa: E402


def main() -> int:
    ok = True

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        print(("PASS " if condition else "FAIL ") + label)
        ok = ok and condition

    config_store.set_disabled_builtin_extensions(["global.disabled"])
    ordinary = session_store.create_session(
        name="ordinary",
        cwd="/tmp/ordinary-extension-policy",
    )
    check(
        "missing session policy uses global default",
        disabled_builtin_extensions_for_run(
            None,
            session_record={},
            worker_record={},
        ) == ["global.disabled"],
    )
    check(
        "ordinary created session uses global default",
        disabled_builtin_extensions_for_run(
            None,
            session_record=ordinary,
            worker_record={},
        ) == ["global.disabled"],
    )
    check(
        "session policy wins over global default",
        disabled_builtin_extensions_for_run(
            None,
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={},
        ) == ["session.disabled"],
    )
    check(
        "worker policy wins over session policy",
        disabled_builtin_extensions_for_run(
            None,
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": ["worker.disabled"]},
        ) == ["worker.disabled"],
    )
    check(
        "empty worker policy explicitly clears disabled extensions",
        disabled_builtin_extensions_for_run(
            None,
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": []},
        ) == [],
    )
    check(
        "explicit turn override wins over worker policy",
        disabled_builtin_extensions_for_run(
            ["turn.disabled"],
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": ["worker.disabled"]},
        ) == ["turn.disabled"],
    )
    check(
        "explicit empty turn override clears disabled extensions",
        disabled_builtin_extensions_for_run(
            [],
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": ["worker.disabled"]},
        ) == [],
    )
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

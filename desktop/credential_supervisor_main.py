from __future__ import annotations

import os
import sys


def _self_test() -> int:
    from headless_keyring import Keyring

    previous_key = os.environ.get("BETTER_AGENT_HEADLESS_KEYRING_KEY")
    os.environ["BETTER_AGENT_HEADLESS_KEYRING_KEY"] = "self-test-only"
    try:
        Keyring()
    finally:
        if previous_key is None:
            os.environ.pop("BETTER_AGENT_HEADLESS_KEYRING_KEY", None)
        else:
            os.environ["BETTER_AGENT_HEADLESS_KEYRING_KEY"] = previous_key
    return 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return _self_test()
    from browser_backend_supervisor import main as supervisor_main

    return supervisor_main()


if __name__ == "__main__":
    raise SystemExit(main())

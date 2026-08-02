from __future__ import annotations

import os
import sys
import tempfile


def _self_test() -> int:
    from headless_keyring import Keyring

    keys = (
        "BETTER_AGENT_HEADLESS_KEYRING_KEY",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    )
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="better-agent-credential-self-test-") as root:
        os.environ.update({
            "BETTER_AGENT_HEADLESS_KEYRING_KEY": "self-test-only",
            "HOME": root,
            "XDG_CONFIG_HOME": root,
            "XDG_DATA_HOME": root,
        })
        try:
            Keyring()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return _self_test()
    from browser_backend_supervisor import main as supervisor_main

    return supervisor_main()


if __name__ == "__main__":  # pragma: no cover - entry-point guard
    raise SystemExit(main())

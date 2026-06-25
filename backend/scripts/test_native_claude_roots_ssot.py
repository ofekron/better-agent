from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-native-claude-roots-")

import config_store  # noqa: E402
import native_session_miner as nm  # noqa: E402
import paths  # noqa: E402


def main() -> int:
    real_home = Path(_TMP_HOME) / "real-home"
    fake_home = Path(_TMP_HOME) / "fake-home"
    env_home = Path(_TMP_HOME) / "env-claude-home"
    for root in (real_home, fake_home, env_home):
        shutil.rmtree(root, ignore_errors=True)

    expected_default = real_home / ".claude" / "projects"
    expected_zai = real_home / ".claude-zai" / "projects"
    stray_real = real_home / ".claude-old" / "projects"
    stray_fake = fake_home / ".claude-fake" / "projects"
    stray_env = env_home / "projects"
    for root in (expected_default, expected_zai, stray_real, stray_fake, stray_env):
        root.mkdir(parents=True)

    old_home = os.environ.get("HOME")
    old_claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    old_user_home = paths._USER_HOME
    old_list_providers = config_store.list_providers
    try:
        os.environ["HOME"] = str(fake_home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(env_home)
        paths._USER_HOME = real_home
        config_store.list_providers = lambda: {"providers": [
            {"kind": "claude", "config_dir": ""},
            {"kind": "claude", "config_dir": "~/.claude-zai"},
        ]}

        roots = nm._claude_projects_roots()
        assert roots == [expected_default, expected_zai], roots
        assert stray_real not in roots
        assert stray_fake not in roots
        assert stray_env not in roots
        print("PASS: Claude native roots use provider config as SSOT")
        return 0
    finally:
        config_store.list_providers = old_list_providers
        paths._USER_HOME = old_user_home
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_claude_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_claude_config_dir
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

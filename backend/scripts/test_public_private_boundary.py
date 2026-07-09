from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_NAME = "better-agent" + "-private"


def test_public_core_imports_without_private_sibling() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-public-boundary-") as home, tempfile.TemporaryDirectory(
        prefix="ba-public-source-"
    ) as source:
        isolated_root = Path(source)
        shutil.copytree(ROOT / "backend", isolated_root / "backend")
        shutil.copytree(ROOT / "sdk", isolated_root / "sdk")
        env = {
            **os.environ,
            "BETTER_AGENT_HOME": home,
            "BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE": "1",
            "PYTHONPATH": os.pathsep.join((str(isolated_root / "backend"), str(isolated_root / "sdk"))),
        }
        script = "import extension_store, requirement_context; assert extension_store._PRIVATE_REGISTRY['ids'] == {}"
        subprocess.run([sys.executable, "-c", script], cwd=isolated_root, env=env, check=True)


def test_tracked_production_and_tests_do_not_name_private_sibling() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "backend", "frontend/src", "frontend/tests", "extensions", "sdk"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    offenders: list[str] = []
    for relative in tracked:
        path = ROOT / relative
        if not path.is_file() or path == Path(__file__):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if FORBIDDEN_NAME in content:
            offenders.append(relative)
    assert offenders == []

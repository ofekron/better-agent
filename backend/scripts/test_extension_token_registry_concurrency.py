"""Cross-process write race on extension_tokens.json.

The backend AND every runner subprocess persist the extension token
registry. With a fixed tmp filename, concurrent writers collide: writer
A's os.replace consumes the shared tmp out from under writer B's, and B
dies with FileNotFoundError (observed as runner init crashes when several
runs spawn at once). Persisting must use a per-writer tmp so concurrent
write-then-replace pairs never interfere.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-ext-token-race-")

BACKEND = Path(__file__).resolve().parents[1]

_CHILD = """
import sys
sys.path.insert(0, {backend!r})
import extension_token_registry as reg

writer_id = sys.argv[1]
for i in range(300):
    # Bypass the in-process cache/lock wrapper deliberately: the race under
    # test is the cross-process write-then-replace, which mint() reaches via
    # _persist_locked. Each process writes its own payload repeatedly.
    with reg._LOCK:
        reg._persist_locked({{"ext-" + writer_id: "tok-" + str(i)}})
"""


def test_concurrent_processes_persist_without_tmp_collision() -> None:
    child_code = _CHILD.format(backend=str(BACKEND))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", child_code, str(n)],
            env={
                "BETTER_AGENT_HOME": _TMP_HOME,
                "BETTER_CLAUDE_HOME": _TMP_HOME,
                "BETTER_AGENT_TEST_MODE": "1",
                "PATH": "/usr/bin:/bin",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for n in range(3)
    ]
    failures = []
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        if proc.returncode != 0:
            failures.append(err.decode("utf-8", "replace")[-500:])
    assert not failures, (
        "concurrent extension-token writers crashed (tmp collision):\n"
        + "\n---\n".join(failures)
    )


if __name__ == "__main__":
    test_concurrent_processes_persist_without_tmp_collision()
    print("PASS  concurrent extension-token persistence")

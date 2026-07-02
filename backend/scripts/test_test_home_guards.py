"""Regression: a test process can never resolve or delete the real home.

This locks in the three layers added after a leaked `BETTER_AGENT_HOME` let a
test's `shutil.rmtree` destroy the developer's real `~/.better-claude`:

  * `paths.ba_home()` refuses any state root under the OS user home in test mode.
  * the deletion primitives reject any target resolving under that home.
  * the prod-home reference is `pwd`-based, so a spoofed `$HOME` cannot move it.

Run with: cd backend && .venv/bin/python scripts/test_test_home_guards.py
"""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402
import paths  # noqa: E402

_test_home.isolate("bc-test-home-guards-")  # engages sentinel + deletion guard

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, msg: str) -> bool:
    print(("OK  " if cond else "BAD ") + msg)
    return cond


def main() -> int:
    import shutil

    prod = paths.user_home()
    ok = True

    ok &= _check(paths.is_test_mode(), "test mode sentinel is set")
    ok &= _check(prod != Path("/tmp"), "prod home resolved from pwd (not a literal)")

    # ba_home() returns the tempdir, never the prod home.
    home = paths.ba_home()
    ok &= _check(prod not in home.resolve().parents and home != prod,
                 f"ba_home() resolved outside prod home ({home})")

    # Layer 1: ba_home() raises when env is pointed at the real home — the
    # exact shape of the original leak (inherited real BETTER_AGENT_HOME).
    saved = (os.environ.get("BETTER_AGENT_HOME"), os.environ.get("BETTER_CLAUDE_HOME"))
    try:
        os.environ["BETTER_AGENT_HOME"] = str(prod / paths._DEFAULT_STATE_DIR)
        try:
            paths.ba_home()
            raised = False
        except RuntimeError:
            raised = True
        ok &= _check(raised, "ba_home() refuses explicit real-home env (the leak shape)")
    finally:
        os.environ["BETTER_AGENT_HOME"], os.environ["BETTER_CLAUDE_HOME"] = saved
    paths.ba_home()  # restore a valid temp resolution

    # $HOME spoof must not move the guard's reference frame.
    fake = tempfile.mkdtemp(prefix="bc-fake-home-")
    spoof_saved = os.environ.get("HOME")
    try:
        os.environ["HOME"] = fake
        ok &= _check(Path.home() == Path(fake), "Path.home() honors spoofed $HOME")
        ok &= _check(paths.user_home() == prod, "guard reference stays pwd-based under $HOME spoof")
        ok &= _check(_test_home._resolves_under_prod(Path(fake)) is False,
                     "spoofed $HOME is NOT treated as prod home")
    finally:
        if spoof_saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = spoof_saved

    # Layer 2: deletion primitives reject anything resolving under a prod
    # state dir (~/.better-claude / ~/.better-agent). Target a non-existent
    # child so no real file is touched; the guard rejects before any FS op.
    fake_prod_child = prod / paths._DEFAULT_STATE_DIR / "not-a-real-test-target"
    for name, fn in (
        ("rmtree", lambda: shutil.rmtree(fake_prod_child)),
        ("os.remove", lambda: os.remove(fake_prod_child)),
        ("Path.unlink", lambda: fake_prod_child.unlink()),
    ):
        try:
            fn()
            guarded = False
        except RuntimeError:
            guarded = True
        except FileNotFoundError:
            guarded = False  # slipped past the guard — would be a real hole
        ok &= _check(guarded, f"deletion guard blocks {name} under prod home")

    # A normal tempdir delete still works (no false positive).
    tmp = Path(tempfile.mkdtemp(prefix="bc-guard-neg-"))
    _test_home._ORIG_RMTREE(tmp)
    ok &= _check(not tmp.exists(), "tempdir deletion still works (guard is not over-eager)")

    # Fail-closed: an unresolvable target (bytes path) must NOT slip past L3.
    # Pre-fix this returned False and the real home would be deleted.
    try:
        shutil.rmtree(str(prod / paths._DEFAULT_STATE_DIR).encode())
        fail_closed = False
    except RuntimeError:
        fail_closed = True
    ok &= _check(fail_closed, "guard fails CLOSED on unresolvable (bytes) prod target")

    # TestHome.release() must reject a prod-rooted path (public ctor misuse).
    try:
        _test_home.TestHome(str(prod / paths._DEFAULT_STATE_DIR)).release()
        release_guarded = False
    except RuntimeError:
        release_guarded = True
    ok &= _check(release_guarded, "TestHome.release refuses prod-rooted path")

    import session_store

    first_home = paths.ba_home()
    first = session_store.create_session(
        id="home-switch-first",
        name="first",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        provider_id="provider",
    )
    ok &= _check(
        (first_home / "sessions" / f"{first['id']}.json").exists(),
        "session_store writes to the first isolated home",
    )
    _test_home._ORIG_RMTREE(first_home)
    second_home = Path(_test_home.isolate("bc-test-home-switch-"))
    second = session_store.create_session(
        id="home-switch-second",
        name="second",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        provider_id="provider",
    )
    ok &= _check(
        (second_home / "sessions" / f"{second['id']}.json").exists(),
        "session_store follows a later test-home switch after old home deletion",
    )
    ok &= _check(
        not (second_home / "sessions" / f"{first['id']}.json").exists(),
        "session_store clears home-scoped indexes on test-home switch",
    )

    print(PASS if ok else FAIL)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Test desktop/setup.py — osascript dialog-output parsing.

The dialogs themselves are build-verified; `_parse_dialog_output` is the
pure logic that turns `osascript` output into the entered value.

Run with:
    backend/.venv/bin/python desktop/test_setup.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="bc-test-desktop-setup-")
import os
os.environ["BETTER_CLAUDE_HOME"] = _TMP_HOME

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
for _p in (_HERE, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import auth_secrets
import setup as _setup
from setup import _escape_applescript_text, _parse_dialog_button, _parse_dialog_output

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_parses_text_returned() -> bool:
    cases = {
        "button returned:OK, text returned:alice": "alice",
        "button returned:OK, text returned:": "",
        "button returned:OK, text returned:has spaces ": "has spaces",
        "button returned:Cancel": "",
    }
    for stdout, expected in cases.items():
        got = _parse_dialog_output(stdout)
        if got != expected:
            print(f"  {stdout!r}: expected {expected!r}, got {got!r}")
            return False
    return True


def test_parses_button_returned() -> bool:
    cases = {
        "button returned:Host Primary": "Host Primary",
        "button returned:Join as Node": "Join as Node",
        "button returned:OK, text returned:alice": "OK",
        "": "",
    }
    for stdout, expected in cases.items():
        got = _parse_dialog_button(stdout)
        if got != expected:
            print(f"  {stdout!r}: expected {expected!r}, got {got!r}")
            return False
    return True


def test_escape_applescript_text() -> bool:
    got = _escape_applescript_text('Python "app"\nnext\\line')
    expected = 'Python \\"app\\"\\nnext\\\\line'
    if got != expected:
        print(f"  expected {expected!r}, got {got!r}")
        return False
    return True


def _drive(prompt_returns: list):
    """Run `run_setup` with `_prompt` yielding the given sequence, `_alert`
    silenced, and `write_credentials` stubbed (no real keychain write).
    Returns (result, list-of-written-credentials)."""
    seq = list(prompt_returns)
    written: list = []
    orig_prompt, orig_alert = _setup._prompt, _setup._alert
    orig_write = auth_secrets.write_credentials
    _setup._prompt = lambda *a, **k: seq.pop(0)
    _setup._alert = lambda *a, **k: None
    auth_secrets.write_credentials = lambda u, p: written.append((u, p))
    try:
        return _setup.run_setup(), written
    finally:
        _setup._prompt, _setup._alert = orig_prompt, orig_alert
        auth_secrets.write_credentials = orig_write


def test_cancel_at_username() -> bool:
    result, written = _drive([None])
    if result is not False or written:
        print(f"  expected (False, []), got ({result}, {written})")
        return False
    return True


def test_empty_username_aborts() -> bool:
    result, written = _drive([""])
    if result is not False or written:
        print(f"  expected (False, []), got ({result}, {written})")
        return False
    return True


def test_cancel_at_password() -> bool:
    result, written = _drive(["alice", None])
    if result is not False or written:
        print(f"  expected (False, []), got ({result}, {written})")
        return False
    return True


def test_mismatch_retries_then_succeeds() -> bool:
    # username, password, confirm(mismatch) → loop → password, confirm(match)
    result, written = _drive(["alice", "p1", "BAD", "p2", "p2"])
    if result is not True or written != [("alice", "p2")]:
        print(f"  expected (True, [('alice','p2')]), got ({result}, {written})")
        return False
    return True


def test_desktop_role_asks_once_then_persists() -> bool:
    path = _setup._desktop_role_path()
    path.unlink(missing_ok=True)
    calls = []
    orig_choose = _setup._choose_role
    _setup._choose_role = lambda: calls.append("asked") or "node"
    try:
        first = _setup.ensure_desktop_role()
        second = _setup.ensure_desktop_role()
    finally:
        _setup._choose_role = orig_choose
        path.unlink(missing_ok=True)
    if first != "node" or second != "node" or calls != ["asked"]:
        print(f"  expected persisted node role, got {first}, {second}, {calls}")
        return False
    return True


def test_primary_bind_asks_once_then_sets_pref() -> bool:
    path = _setup._primary_bind_configured_path()
    path.unlink(missing_ok=True)
    calls = []
    orig_choose = _setup._choose_primary_bind
    _setup._choose_primary_bind = lambda: calls.append("asked") or "0.0.0.0"
    try:
        first = _setup.ensure_primary_network_bind()
        second = _setup.ensure_primary_network_bind()
        import user_prefs
        address = user_prefs.get_network_bind_address()
    finally:
        _setup._choose_primary_bind = orig_choose
        path.unlink(missing_ok=True)
    if first is not True or second is not True or calls != ["asked"]:
        print(f"  expected prompt once and success, got {first}, {second}, {calls}")
        return False
    if address != "0.0.0.0":
        print(f"  expected bind pref 0.0.0.0, got {address}")
        return False
    return True


def test_primary_bind_cancel_aborts_without_sentinel() -> bool:
    path = _setup._primary_bind_configured_path()
    path.unlink(missing_ok=True)
    orig_choose = _setup._choose_primary_bind
    _setup._choose_primary_bind = lambda: None
    try:
        result = _setup.ensure_primary_network_bind()
    finally:
        _setup._choose_primary_bind = orig_choose
    if result is not False:
        print(f"  expected False, got {result}")
        return False
    if path.exists():
        print("  sentinel should not be written after cancel")
        return False
    return True


def test_resolve_port_conflict_kill_choice() -> bool:
    orig_choose = _setup._choose_port_conflict
    _setup._choose_port_conflict = lambda port, listeners: "kill"
    try:
        result = _setup.resolve_port_conflict(
            8000, [{"pid": 123, "command": "Python"}],
        )
    finally:
        _setup._choose_port_conflict = orig_choose
    if result != {"action": "kill", "port": 8000}:
        print(f"  expected kill resolution, got {result}")
        return False
    return True


def test_resolve_port_conflict_alternate_port() -> bool:
    orig_choose, orig_prompt = _setup._choose_port_conflict, _setup._prompt
    _setup._choose_port_conflict = lambda port, listeners: "use_port"
    _setup._prompt = lambda *a, **k: "9000"
    try:
        result = _setup.resolve_port_conflict(
            8000, [{"pid": 123, "command": "Python"}],
        )
    finally:
        _setup._choose_port_conflict = orig_choose
        _setup._prompt = orig_prompt
    if result != {"action": "use_port", "port": 9000}:
        print(f"  expected alternate port resolution, got {result}")
        return False
    return True


def test_invalid_desktop_role_fails_closed() -> bool:
    path = _setup._desktop_role_path()
    path.write_text("bad\n", encoding="utf-8")
    try:
        try:
            _setup.ensure_desktop_role()
        except RuntimeError:
            return True
        print("  expected RuntimeError")
        return False
    finally:
        path.unlink(missing_ok=True)


def test_normalize_primary_address() -> bool:
    cases = {
        "100.1.2.3": "ws://100.1.2.3:8000",
        "host.local:9000": "ws://host.local:9000",
        "http://host.local": "ws://host.local:8000",
        "https://host.local:8443": "wss://host.local:8443",
        "ws://host.local:8000": "ws://host.local:8000",
    }
    for raw, expected in cases.items():
        got = _setup._normalize_primary_address(raw)
        if got != expected:
            print(f"  {raw!r}: expected {expected!r}, got {got!r}")
            return False
    return True


def test_ensure_node_topology_prompts_once() -> bool:
    path = _setup._topology_path()
    path.unlink(missing_ok=True)
    calls = []
    orig_prompt, orig_alert = _setup._prompt, _setup._alert
    orig_post_json, orig_get_json = _setup._post_json, _setup._get_json
    answers = iter(["primary.local", "alice", "secret"])

    def prompt(*a, **k):
        calls.append(a[0])
        return next(answers)

    _setup._prompt = prompt
    _setup._alert = lambda *a, **k: None
    _setup._post_json = lambda url, payload, *, token="": (
        200,
        {"token": "primary-token"},
    )
    _setup._get_json = lambda url, *, token="": (200, [])
    try:
        first = _setup.ensure_node_topology()
        second = _setup.ensure_node_topology()
        content = path.read_text(encoding="utf-8")
    finally:
        _setup._prompt, _setup._alert = orig_prompt, orig_alert
        _setup._post_json, _setup._get_json = orig_post_json, orig_get_json
        path.unlink(missing_ok=True)
    if not first or not second or calls != [
        "Enter the primary machine address or IP (port 8000 is used when omitted):",
        "Primary username:",
        "Primary password:",
    ]:
        print(f"  expected prompt once and success, got {first}, {second}, {calls}")
        return False
    if "address: ws://primary.local:8000" not in content:
        print(f"  topology content missing normalized address: {content}")
        return False
    if os.environ.get("BETTER_CLAUDE_TOPOLOGY_PATH") != str(path):
        print("  topology env var was not exported for node backend")
        return False
    return True


def test_ensure_node_topology_blocks_when_primary_lacks_extension() -> bool:
    path = _setup._topology_path()
    path.unlink(missing_ok=True)
    alerts = []
    orig_prompt, orig_alert = _setup._prompt, _setup._alert
    orig_post_json, orig_get_json = _setup._post_json, _setup._get_json
    answers = iter(["primary.local", "alice", "secret"])
    _setup._prompt = lambda *a, **k: next(answers)
    _setup._alert = lambda msg: alerts.append(msg)
    _setup._post_json = lambda url, payload, *, token="": (
        200,
        {"token": "primary-token"},
    )
    _setup._get_json = lambda url, *, token="": (
        404,
        {"detail": "Extension is not installed"},
    )
    try:
        ok = _setup.ensure_node_topology()
    finally:
        _setup._prompt, _setup._alert = orig_prompt, orig_alert
        _setup._post_json, _setup._get_json = orig_post_json, orig_get_json
        path.unlink(missing_ok=True)
    if ok:
        print("  expected setup to fail")
        return False
    if path.exists():
        print("  topology should not be written when primary is not ready")
        return False
    if not alerts or "Machine nodes" not in alerts[-1]:
        print(f"  expected Machine nodes install guidance, got {alerts}")
        return False
    return True


def _check_dispatch(is_macos: bool) -> bool:
    """_prompt/_alert must route to the osascript impls on macOS and the
    tkinter impls elsewhere — without opening a real window."""
    calls = []
    orig = (
        _setup._is_macos, _setup._prompt_macos, _setup._prompt_tk,
        _setup._alert_macos, _setup._alert_tk,
        _setup._choose_port_conflict_macos, _setup._choose_port_conflict_tk,
    )
    _setup._is_macos = lambda: is_macos
    _setup._prompt_macos = lambda m, *, hidden, default="": calls.append(("p_mac", hidden, default)) or "x"
    _setup._prompt_tk = lambda m, *, hidden, default="": calls.append(("p_tk", hidden, default)) or "x"
    _setup._alert_macos = lambda m: calls.append(("a_mac",))
    _setup._alert_tk = lambda m: calls.append(("a_tk",))
    _setup._choose_port_conflict_macos = (
        lambda port, text: calls.append(("c_mac", port)) or "kill"
    )
    _setup._choose_port_conflict_tk = (
        lambda port, text: calls.append(("c_tk", port)) or "kill"
    )
    try:
        _setup._prompt("hi", hidden=True, default="prefill")
        _setup._alert("oops")
        _setup._choose_port_conflict(8000, "listener")
    finally:
        (
            _setup._is_macos, _setup._prompt_macos, _setup._prompt_tk,
            _setup._alert_macos, _setup._alert_tk,
            _setup._choose_port_conflict_macos,
            _setup._choose_port_conflict_tk,
        ) = orig
    want = (
        [("p_mac", True, "prefill"), ("a_mac",), ("c_mac", 8000)]
        if is_macos
        else [("p_tk", True, "prefill"), ("a_tk",), ("c_tk", 8000)]
    )
    if calls != want:
        print(f"  is_macos={is_macos}: expected {want}, got {calls}")
        return False
    return True


def test_dispatch_macos() -> bool:
    return _check_dispatch(is_macos=True)


def test_dispatch_non_macos() -> bool:
    return _check_dispatch(is_macos=False)


TESTS = [
    ("_parse_dialog_output extracts the entered text", test_parses_text_returned),
    ("_parse_dialog_button extracts the clicked button", test_parses_button_returned),
    ("_escape_applescript_text escapes listener details", test_escape_applescript_text),
    ("_prompt/_alert dispatch to osascript on macOS", test_dispatch_macos),
    ("_prompt/_alert dispatch to tkinter off macOS", test_dispatch_non_macos),
    ("desktop role prompt persists after first launch", test_desktop_role_asks_once_then_persists),
    ("primary bind prompt persists and writes user preference", test_primary_bind_asks_once_then_sets_pref),
    ("primary bind cancel aborts without sentinel", test_primary_bind_cancel_aborts_without_sentinel),
    ("port conflict kill choice resolves to kill", test_resolve_port_conflict_kill_choice),
    ("port conflict alternate choice validates selected port", test_resolve_port_conflict_alternate_port),
    ("invalid desktop role fails closed", test_invalid_desktop_role_fails_closed),
    ("primary address normalization adds scheme and port", test_normalize_primary_address),
    ("node topology generation prompts only when missing", test_ensure_node_topology_prompts_once),
    ("node topology blocks when primary lacks Machine nodes",
     test_ensure_node_topology_blocks_when_primary_lacks_extension),
    ("run_setup returns False when username prompt is cancelled",
     test_cancel_at_username),
    ("run_setup aborts on an empty username", test_empty_username_aborts),
    ("run_setup returns False when password prompt is cancelled",
     test_cancel_at_password),
    ("run_setup retries on password mismatch then succeeds",
     test_mismatch_retries_then_succeeds),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())

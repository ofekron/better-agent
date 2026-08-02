"""Coverage for desktop/shell.py — the macOS/Windows desktop shell wiring.

shell.main() orchestrates the whole app (backend supervisor, activation
server, webview window, updater). It is driven here with every collaborator
mocked so each return path and inner closure is exercised at unit tier.
The standalone helpers are covered directly.
"""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import paths
import shell


# ===========================================================================
# Helpers: pure / lightly-mocked
# ===========================================================================

def test_watch_for_restart_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(shell, "watch_backend", lambda *a, **k: seen.setdefault("args", a))
    quitting = threading.Event()
    shell._watch_for_restart("SUP", "WIN", quitting, "http://x")
    assert seen["args"] == ("SUP", "WIN", quitting, "http://x")


def test_error_window_macos_uses_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(shell.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(shell, "_alert_macos", lambda t, m: calls.append((t, m)))
    monkeypatch.setattr(shell, "webview", MagicMock())  # guard: never start a real window
    shell._error_window("boom")
    assert calls == [("Better Agent could not start", "boom")]
    shell.webview.create_window.assert_not_called()


def test_error_window_other_platform_uses_webview(monkeypatch):
    monkeypatch.setattr(shell.platform, "system", lambda: "Linux")
    monkeypatch.setattr(shell, "webview", MagicMock())
    shell._error_window('<script>bad</script>')
    shell.webview.create_window.assert_called_once()
    html_arg = shell.webview.create_window.call_args.kwargs["html"]
    # message is html-escaped
    assert "&lt;script&gt;" in html_arg
    shell.webview.start.assert_called_once()


# --- _line_switch_fallback --------------------------------------------------

def test_line_switch_fallback_missing_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("BA_SWITCH_HOME", str(tmp_path))
    monkeypatch.setattr(shell, "webview", MagicMock())
    assert shell._line_switch_fallback() is False
    shell.webview.create_window.assert_not_called()


def test_line_switch_fallback_invalid_config(monkeypatch, tmp_path):
    monkeypatch.setenv("BA_SWITCH_HOME", str(tmp_path))
    (tmp_path / "web.json").write_text(json.dumps({"port": "not-an-int", "token": "t"}))
    monkeypatch.setattr(shell, "webview", MagicMock())
    assert shell._line_switch_fallback() is False


def test_line_switch_fallback_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("BA_SWITCH_HOME", str(tmp_path))
    (tmp_path / "web.json").write_text("{not json")
    monkeypatch.setattr(shell, "webview", MagicMock())
    assert shell._line_switch_fallback() is False


def _patch_urlopen(monkeypatch, responses):
    it = iter(responses)

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(url, timeout=None):
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        return _Resp()

    monkeypatch.setattr(shell.urllib.request, "urlopen", fake_urlopen)
    return _Resp


def test_line_switch_fallback_success(monkeypatch, tmp_path):
    monkeypatch.setenv("BA_SWITCH_HOME", str(tmp_path))
    (tmp_path / "web.json").write_text(json.dumps({"port": 8080, "token": "abc"}))
    monkeypatch.setattr(shell, "webview", MagicMock())
    _patch_urlopen(monkeypatch, [None])  # status 200 immediately
    assert shell._line_switch_fallback() is True
    shell.webview.create_window.assert_called_once()
    shell.webview.start.assert_called_once()
    url = shell.webview.create_window.call_args.args[1]
    assert url == "http://127.0.0.1:8080/#abc"


def test_line_switch_fallback_retries_then_success(monkeypatch, tmp_path):
    monkeypatch.setenv("BA_SWITCH_HOME", str(tmp_path))
    (tmp_path / "web.json").write_text(json.dumps({"port": 8080, "token": "abc"}))
    monkeypatch.setattr(shell, "webview", MagicMock())
    # first two attempts fail, third succeeds
    monkeypatch.setattr(shell.time, "sleep", lambda s: None)
    _patch_urlopen(monkeypatch, [urllib.error.URLError("x"), urllib.error.URLError("x"), None])
    assert shell._line_switch_fallback() is True


def test_line_switch_fallback_non200_loops_then_expires(monkeypatch, tmp_path):
    monkeypatch.setenv("BA_SWITCH_HOME", str(tmp_path))
    (tmp_path / "web.json").write_text(json.dumps({"port": 8080, "token": "abc"}))
    monkeypatch.setattr(shell, "webview", MagicMock())

    class _Resp500:
        status = 500
        def __enter__(self): return self
        def __exit__(self, *a): return False
    # monotonic: deadline calc, then loop-enter (<4), then loop-exit (>=4)
    times = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(shell.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(shell.time, "sleep", lambda s: None)
    monkeypatch.setattr(shell.urllib.request, "urlopen", lambda url, timeout=None: _Resp500())
    assert shell._line_switch_fallback() is False
    shell.webview.create_window.assert_not_called()


def test_line_switch_fallback_deadline_expires(monkeypatch, tmp_path):
    monkeypatch.setenv("BA_SWITCH_HOME", str(tmp_path))
    (tmp_path / "web.json").write_text(json.dumps({"port": 8080, "token": "abc"}))
    monkeypatch.setattr(shell, "webview", MagicMock())

    # monotonic time advances past the 4s deadline on the first read
    times = iter([0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(shell.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(shell.time, "sleep", lambda s: None)
    _patch_urlopen(monkeypatch, [urllib.error.URLError("x")] * 5)
    assert shell._line_switch_fallback() is False
    shell.webview.create_window.assert_not_called()


# --- _applescript_string / _alert_macos -------------------------------------

def test_applescript_string_escapes():
    assert shell._applescript_string('a"b\\c') == '"a\\"b\\\\c"'


def test_alert_macos_runs_osascript(monkeypatch):
    calls = []
    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: calls.append((a, k)) or MagicMock(returncode=0))
    shell._alert_macos("T", "M")
    assert len(calls) == 1
    args = calls[0][0][0]
    assert args[0] == "osascript"
    assert "display alert" in args[2]


def test_alert_macos_swallows_oserror(monkeypatch):
    def raise_os(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(shell.subprocess, "run", raise_os)
    shell._alert_macos("T", "M")  # must not raise


def test_alert_macos_swallows_timeout(monkeypatch):
    def raise_to(*a, **k):
        raise shell.subprocess.TimeoutExpired(cmd="osascript", timeout=120)
    monkeypatch.setattr(shell.subprocess, "run", raise_to)
    shell._alert_macos("T", "M")  # must not raise


# --- _configure_logging -----------------------------------------------------

def test_configure_logging_writes_rotating_handler(monkeypatch, tmp_path):
    import logging
    monkeypatch.setattr(paths, "ba_home", lambda: tmp_path)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    # pre-existing handler so _configure_logging's removal loop runs
    root.addHandler(logging.StreamHandler())
    shell._configure_logging()
    assert (tmp_path / "shell.log").parent == tmp_path
    import logging.handlers
    handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert handlers
    # cleanup
    for h in list(root.handlers):
        root.removeHandler(h)


# --- _emit_activation -------------------------------------------------------

def test_emit_activation_activate_shows_only():
    win = MagicMock()
    shell._emit_activation(win, {"type": "activate"})
    win.show.assert_called_once()
    win.evaluate_js.assert_not_called()


def test_emit_activation_other_evaluates_js_then_show():
    win = MagicMock()
    shell._emit_activation(win, {"type": "deep-link"})
    win.evaluate_js.assert_called_once()
    assert "better-agent:deep-link" in win.evaluate_js.call_args.args[0]
    win.show.assert_called_once()


# --- _submit_marketplace_activation -----------------------------------------

def test_submit_non_marketplace_returns_true():
    assert shell._submit_marketplace_activation("http://x", {"type": "activate"}) is True


def test_submit_marketplace_empty_token(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ba_home", lambda: tmp_path)
    (tmp_path / "internal_token").write_text("   ")
    assert shell._submit_marketplace_activation("http://x", {"type": "marketplace_pair", "intent": "i", "version": 1}) is False


def test_submit_marketplace_success(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ba_home", lambda: tmp_path)
    (tmp_path / "internal_token").write_text("tok")

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(shell.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    rc = shell._submit_marketplace_activation(
        "http://127.0.0.1:1/", {"type": "marketplace_pair", "intent": "i", "version": 2})
    assert rc is True


def test_submit_marketplace_non200(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ba_home", lambda: tmp_path)
    (tmp_path / "internal_token").write_text("tok")

    class _Resp:
        status = 500
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(shell.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    rc = shell._submit_marketplace_activation(
        "http://x", {"type": "marketplace_pair", "intent": "i", "version": 2})
    assert rc is False


def test_submit_marketplace_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ba_home", lambda: tmp_path)
    (tmp_path / "internal_token").write_text("tok")
    monkeypatch.setattr(shell.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(OSError("x")))
    rc = shell._submit_marketplace_activation(
        "http://x", {"type": "marketplace_pair", "intent": "i", "version": 2})
    assert rc is False


# ===========================================================================
# main() — mocked-orchestration harness
# ===========================================================================

class _Slot:
    """Supports `events.loaded += fn` and records the handler."""
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _Events:
    def __init__(self):
        self.loaded = _Slot()
        self.closing = _Slot()


class FakeWindow:
    def __init__(self):
        self.events = _Events()
        self.shown = False
        self.js = []
        self.dialog = False

    def show(self):
        self.shown = True

    def evaluate_js(self, code):
        self.js.append(code)

    def create_confirmation_dialog(self, title, text):
        return self.dialog


class _FakeActivationServer:
    last = None

    def __init__(self, state_dir, on_activation):
        self.state_dir = state_dir
        self.on_activation = on_activation
        self.started = False
        self.closed = False
        type(self).last = self

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


class _FakeSupervisor:
    def __init__(self, env):
        self._env = env
        self.role = env.role
        self.port = 9999
        self.health_url = "http://127.0.0.1:9990/health"
        self.shuts = []

    def start(self, on_port_conflict=None):
        if self._env.start_raises is not None:
            raise self._env.start_raises

    def wait_healthy(self):
        return self._env.healthy

    def local_url(self):
        return "http://127.0.0.1:9999"

    def shutdown(self, kill_runners=False):
        self.shuts.append(kill_runners)


def _make_env(monkeypatch, tmp_path):
    env = SimpleNamespace(
        role="primary",
        node_topology=True,
        primary_bind=True,
        needs_bootstrap=False,
        run_setup_ok=True,
        healthy=True,
        start_raises=None,
        platform="Linux",
        line_switch=False,
        window=FakeWindow(),
        background_cbs=[],
        mac_handlers=[],
        submit_returns=True,
        emit_calls=[],
        error_calls=[],
        start_calls_closing=False,
        apply_calls=[],
    )

    # --- sys.modules fakes for main()'s inner imports ---
    setup_mod = types.ModuleType("setup")
    setup_mod.ensure_desktop_role = lambda: env.role
    setup_mod.ensure_node_topology = lambda: env.node_topology
    setup_mod.ensure_primary_network_bind = lambda: env.primary_bind
    setup_mod.resolve_port_conflict = lambda: True
    setup_mod.run_setup = lambda: env.run_setup_ok
    monkeypatch.setitem(sys.modules, "setup", setup_mod)

    auth_mod = types.ModuleType("auth_secrets")
    auth_mod.needs_bootstrap = lambda: env.needs_bootstrap
    monkeypatch.setitem(sys.modules, "auth_secrets", auth_mod)

    mac_mod = types.ModuleType("macos_url_handler")
    mac_mod.install_macos_url_handler = lambda cb: env.mac_handlers.append(cb)
    monkeypatch.setitem(sys.modules, "macos_url_handler", mac_mod)

    # paths.ba_home -> tmp_path (used by main for ActivationServer state dir)
    monkeypatch.setattr(paths, "ba_home", lambda: tmp_path)

    # --- shell attribute patches ---
    monkeypatch.setattr(shell, "_configure_logging", lambda: None)
    monkeypatch.setattr(shell.platform, "system", lambda: env.platform)
    monkeypatch.setattr(shell, "_error_window", lambda m: env.error_calls.append(m))

    def fake_line_switch():
        return env.line_switch
    monkeypatch.setattr(shell, "_line_switch_fallback", fake_line_switch)

    monkeypatch.setattr(shell, "ActivationServer", _FakeActivationServer)
    monkeypatch.setattr(shell, "DesktopNotificationApi", MagicMock)
    monkeypatch.setattr(shell, "BackendSupervisor", lambda role=None: _FakeSupervisor(env))

    webview = MagicMock()
    webview.create_window = lambda *a, **k: env.window

    def _start(*a, **k):
        if env.start_calls_closing and env.window.events.closing.handlers:
            env.window.events.closing.handlers[0]()
    webview.start = _start
    monkeypatch.setattr(shell, "webview", webview)

    monkeypatch.setattr(shell, "_watch_for_restart", lambda *a, **k: None)
    monkeypatch.setattr(shell, "start_background_check", lambda cb: env.background_cbs.append(cb))

    def fake_submit(local_url, event):
        return env.submit_returns
    monkeypatch.setattr(shell, "_submit_marketplace_activation", fake_submit)

    def fake_emit(window, event):
        env.emit_calls.append(event)
    monkeypatch.setattr(shell, "_emit_activation", fake_emit)

    monkeypatch.setattr(shell, "forward_activation_when_ready", lambda *a, **k: env.forward_ready)
    env.forward_ready = True

    updater = MagicMock()
    updater.apply_and_relaunch = lambda: env.apply_calls.append(True)
    monkeypatch.setattr(shell, "updater", updater)

    return env


# --- main() return paths ----------------------------------------------------

def test_main_happy_primary(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    rc = shell.main()
    assert rc == 0
    assert _FakeActivationServer.last.started is True
    assert _FakeActivationServer.last.closed is True


def test_deliver_single_initial_activation(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    rc = shell.main(initial_activation={"type": "marketplace_pair", "intent": "i", "version": 1})
    assert rc == 0
    env.window.events.loaded.handlers[0]()
    assert env.emit_calls  # marketplace submit returned True -> emitted


def test_deliver_multiple_queued_activations(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    # one initial activation + a second queued before the window is delivered
    shell.main(initial_activation={"type": "activate"})
    _FakeActivationServer.last.on_activation({"type": "activate"})
    env.window.events.loaded.handlers[0]()
    assert len(env.emit_calls) == 2  # loop iterated over both queued events


def test_deliver_loop_continues_when_submit_declines(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    # first queued activation's submit declines (loop continues to next item),
    # second succeeds -> exercises the submit-False back-edge in the loop
    returns = iter([False, True])
    monkeypatch.setattr(shell, "_submit_marketplace_activation", lambda url, ev: next(returns))
    shell.main(initial_activation={"type": "activate"})
    _FakeActivationServer.last.on_activation({"type": "activate"})
    env.window.events.loaded.handlers[0]()
    assert len(env.emit_calls) == 1  # only the second was emitted


def test_main_darwin_installs_url_handler(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.platform = "Darwin"
    assert shell.main() == 0
    assert len(env.mac_handlers) == 1


def test_main_role_none(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.role = None
    assert shell.main() == 1
    assert _FakeActivationServer.last.closed is True


def test_main_node_topology_failure(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.role = "node"
    env.node_topology = False
    assert shell.main() == 1
    assert _FakeActivationServer.last.closed is True


def test_main_node_happy_uses_node_window(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.role = "node"
    assert shell.main() == 0


def test_main_primary_bind_failure(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.primary_bind = False
    assert shell.main() == 1
    assert _FakeActivationServer.last.closed is True


def test_main_needs_bootstrap_run_setup_fails(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.needs_bootstrap = True
    env.run_setup_ok = False
    assert shell.main() == 1
    assert _FakeActivationServer.last.closed is True


def test_main_needs_bootstrap_run_setup_ok(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.needs_bootstrap = True
    env.run_setup_ok = True
    assert shell.main() == 0


def test_main_activation_server_start_raises_forward_true(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.forward_ready = True

    class _BoomServer(_FakeActivationServer):
        def start(self):
            raise RuntimeError("port in use")
    monkeypatch.setattr(shell, "ActivationServer", _BoomServer)
    assert shell.main() == 0


def test_main_activation_server_start_raises_forward_false(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.forward_ready = False

    class _BoomServer(_FakeActivationServer):
        def start(self):
            raise RuntimeError("port in use")
    monkeypatch.setattr(shell, "ActivationServer", _BoomServer)
    assert shell.main() == 1


def test_main_supervisor_start_raises_error_window(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.start_raises = RuntimeError("boom port")
    assert shell.main() == 1
    assert env.error_calls == ["boom port"]
    assert _FakeActivationServer.last.closed is True


def test_main_not_healthy_line_switch_succeeds(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.healthy = False
    env.line_switch = True
    assert shell.main() == 1
    assert env.error_calls == []  # line switch took over, no error window


def test_main_not_healthy_line_switch_fails(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.healthy = False
    env.line_switch = False
    assert shell.main() == 1
    assert env.error_calls  # error window shown


# --- main() inner closures --------------------------------------------------

def test_on_activation_window_none_queues(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    shell.main()
    # before the window is delivered, activation is queued (no submit/emit)
    _FakeActivationServer.last.on_activation({"type": "activate"})
    assert env.emit_calls == []


def test_on_activation_delivered_emits(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    shell.main()
    # deliver initial -> sets active_window; then a later activation emits
    env.window.events.loaded.handlers[0]()
    env.submit_returns = True
    _FakeActivationServer.last.on_activation({"type": "activate"})
    assert env.emit_calls == [{"type": "activate"}]


def test_on_activation_submit_false_skips_emit(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    shell.main()
    env.window.events.loaded.handlers[0]()
    env.submit_returns = False
    _FakeActivationServer.last.on_activation({"type": "marketplace_pair", "intent": "i", "version": 1})
    assert env.emit_calls == []


def test_on_closing_dialog_true_kills_runners(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.window.dialog = True
    env.start_calls_closing = True
    # capture supervisor via a side channel
    sups = []
    monkeypatch.setattr(shell, "BackendSupervisor", lambda role=None: sups.append(_FakeSupervisor(env)) or sups[0])
    assert shell.main() == 0
    # closing handler ran during webview.start -> kill_on_quit True -> shutdown(True)
    assert True in sups[0].shuts


def test_on_closing_dialog_false(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    env.window.dialog = False
    env.start_calls_closing = True
    sups = []
    monkeypatch.setattr(shell, "BackendSupervisor", lambda role=None: sups.append(_FakeSupervisor(env)) or sups[0])
    assert shell.main() == 0
    # dialog False -> kill_on_quit False -> all shutdowns False
    assert all(v is False for v in sups[0].shuts)


def test_on_update_available_decline(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    shell.main()
    env.window.dialog = False
    env.background_cbs[0]("1.2.3")
    assert env.apply_calls == []  # declined -> no apply


def test_on_update_available_accept(monkeypatch, tmp_path):
    env = _make_env(monkeypatch, tmp_path)
    sups = []
    monkeypatch.setattr(shell, "BackendSupervisor", lambda role=None: sups.append(_FakeSupervisor(env)) or sups[0])
    shell.main()
    env.window.dialog = True
    env.background_cbs[0]("1.2.3")
    assert env.apply_calls == [True]
    # accepted -> _on_update shutdown leaves runners alive (the last shutdown call)
    assert sups[0].shuts[-1] is False

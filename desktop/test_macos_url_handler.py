"""Unit coverage for desktop/macos_url_handler.py — the macOS URL-scheme
delegate that turns inbound deep links into ActivationEvents.

install_macos_url_handler builds a BrowserView.AppDelegate subclass whose
application_openURLs_ parses each URL via parse_deep_link, dispatches the
resulting event to on_event, and swallows DeepLinkError / ValueError. The
webview import lives inside the function body, so the cocoa module is injected
through sys.modules; parse_deep_link / DeepLinkError are patched at the
module seam.

Run:
    cd desktop && ../backend/.venv/bin/python -m pytest test_macos_url_handler.py \
        --cov=macos_url_handler --cov-report=term-missing --cov-branch
"""
from __future__ import annotations

import sys
import types
from unittest import mock

import macos_url_handler


class _FakeAllocator:
    """Mimics objc ``.alloc().init()``: alloc()->allocator, init()->instance."""

    def __init__(self, cls):
        self._cls = cls

    def init(self):  # noqa: D401 - objc shape
        return self._cls()


class FakeAppDelegate:
    """Stand-in for webview's BrowserView.AppDelegate. Captures the subclass
    defined inside install_macos_url_handler and provides the alloc().init()
    chain so the shared-delegate assignment resolves to a real instance."""

    last_subclass = None

    def __init_subclass__(cls, **kwargs):  # noqa: D401 - record subclass
        FakeAppDelegate.last_subclass = cls

    @classmethod
    def alloc(cls):
        return _FakeAllocator(cls)


class FakeBrowserView:
    AppDelegate = FakeAppDelegate
    _shared_app_delegate = None


class _FakeURL:
    def __init__(self, absolute_string: str):
        self._s = absolute_string

    def absoluteString(self):  # noqa: D401 - cocoa selector shape
        return self._s


class _ObjcNSString:
    """Mimics cocoa absoluteString() returning a non-str objc object — the
    delegate must str()-coerce it before handing it to parse_deep_link."""

    def __init__(self, value: str):
        self._value = value

    def __str__(self):
        return self._value


class _ObjcURL:
    def __init__(self, value: str):
        self._value = value

    def absoluteString(self):  # noqa: D401 - cocoa selector shape
        return _ObjcNSString(self._value)


def _install_cocoa(monkeypatch):
    cocoa = types.ModuleType("webview.platforms.cocoa")
    cocoa.BrowserView = FakeBrowserView
    webview_pkg = types.ModuleType("webview")
    webview_pkg.platforms = types.ModuleType("webview.platforms")
    monkeypatch.setitem(sys.modules, "webview", webview_pkg)
    monkeypatch.setitem(sys.modules, "webview.platforms", webview_pkg.platforms)
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", cocoa)
    return cocoa


def _delegate_instance():
    """Returns a fresh instance of the subclass install_macos_url_handler just
    registered, with application_openURLs_ ready to drive."""
    assert FakeAppDelegate.last_subclass is not None, "delegate not installed"
    return FakeAppDelegate.last_subclass()


def test_install_registers_shared_delegate(monkeypatch):
    cocoa = _install_cocoa(monkeypatch)
    on_event = mock.MagicMock()
    macos_url_handler.install_macos_url_handler(on_event)

    # The subclass was defined and the shared delegate assigned to an instance.
    assert FakeAppDelegate.last_subclass is not None
    assigned = cocoa.BrowserView._shared_app_delegate
    assert isinstance(assigned, FakeAppDelegate.last_subclass)


def test_valid_deep_link_dispatches_event(monkeypatch):
    _install_cocoa(monkeypatch)
    received = []

    parsed = mock.MagicMock()
    parsed.as_event.return_value = {"type": "marketplace_pair", "intent": "x"}
    with mock.patch.object(macos_url_handler, "parse_deep_link",
                           return_value=parsed) as parse:
        macos_url_handler.install_macos_url_handler(received.append)
        delegate = _delegate_instance()
        delegate.application_openURLs_(
            None, [_FakeURL("better-agent://x"), _FakeURL("better-agent://y")])

    # Each URL parsed via its absoluteString and dispatched exactly once.
    assert [c.args[0] for c in parse.call_args_list] == ["better-agent://x",
                                                         "better-agent://y"]
    assert received == [
        {"type": "marketplace_pair", "intent": "x"},
        {"type": "marketplace_pair", "intent": "x"},
    ]


def test_deep_link_error_is_swallowed(monkeypatch):
    _install_cocoa(monkeypatch)
    on_event = mock.MagicMock()
    with mock.patch.object(macos_url_handler, "parse_deep_link",
                           side_effect=macos_url_handler.DeepLinkError("bad")):
        macos_url_handler.install_macos_url_handler(on_event)
        delegate = _delegate_instance()
        # Must not raise; on_event never called.
        delegate.application_openURLs_(None, [_FakeURL("better-agent://bad")])
    on_event.assert_not_called()


def test_value_error_is_swallowed(monkeypatch):
    _install_cocoa(monkeypatch)
    on_event = mock.MagicMock()
    with mock.patch.object(macos_url_handler, "parse_deep_link",
                           side_effect=ValueError("nope")):
        macos_url_handler.install_macos_url_handler(on_event)
        delegate = _delegate_instance()
        delegate.application_openURLs_(None, [_FakeURL("better-agent://nope")])
    on_event.assert_not_called()


def test_mixed_urls_dispatch_only_valid(monkeypatch):
    _install_cocoa(monkeypatch)
    received = []
    good = mock.MagicMock()
    good.as_event.return_value = {"type": "marketplace_pair", "intent": "ok"}

    def parse(value):
        if value == "drop":
            raise ValueError("drop it")
        return good

    with mock.patch.object(macos_url_handler, "parse_deep_link",
                           side_effect=parse):
        macos_url_handler.install_macos_url_handler(received.append)
        delegate = _delegate_instance()
        delegate.application_openURLs_(
            None, [_FakeURL("ok1"), _FakeURL("drop"), _FakeURL("ok2")])
    # Two valid events dispatched; the bad middle URL swallowed.
    assert len(received) == 2


def test_absolute_string_is_str_coerced(monkeypatch):
    """cocoa absoluteString() returns a non-str objc object; the delegate must
    str()-coerce it before parse_deep_link."""
    _install_cocoa(monkeypatch)
    received = []
    captured = []
    good = mock.MagicMock()
    good.as_event.return_value = {"type": "marketplace_pair"}

    def parse(value):
        captured.append(value)
        return good

    with mock.patch.object(macos_url_handler, "parse_deep_link",
                           side_effect=parse):
        macos_url_handler.install_macos_url_handler(received.append)
        delegate = _delegate_instance()
        delegate.application_openURLs_(None, [_ObjcURL("better-agent://objc")])

    assert len(captured) == 1
    assert captured[0] == "better-agent://objc"
    assert isinstance(captured[0], str), "absoluteString must be str-coerced"
    assert received == [{"type": "marketplace_pair"}]

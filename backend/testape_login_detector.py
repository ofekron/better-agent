"""Detect Better Agent's login state by driving a browser through the TestApe SDK.

Single source of truth for "which auth screen is the running app showing right
now". Uses TestApe's WebSession to read live DOM markers from the web app that a
connected TestApe web adapter has open.

States:
  login         — returning-user login form
  setup         — first-run setup form
  authenticated — logged in (app reachable, session valid)
  unknown       — logged out with no login screen, or app unreachable

How it detects (all executed inside the TestApe-driven browser via eval_js):
  Authoritative auth state comes from the app's own /api/auth/me endpoint,
  fetched from within the browser (200 => logged in). That endpoint returns 401
  for both Login and Setup, so the DOM disambiguates them: Login.tsx and
  Setup.tsx render a near-identical DOM (.login-shell > .login-card) and differ
  only in the password field's autoComplete attribute — "current-password"
  (login) vs "new-password" (setup). Global CSS (styles/globals.css) keeps these
  plain, queryable class names.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

FS_DEFAULT = "http://localhost:5056/v1"
APP_URL_DEFAULT = os.environ.get("BETTER_AGENT_APP_URL", "http://localhost:8000")

_BROWSER_HINTS = ("chrome", "chromium", "web", "edge")

# Run inside the browser via TestApe eval_js. Awaits the auth fetch (eval_js
# resolves promises) and reads the DOM markers that disambiguate login/setup.
_DETECT_JS = """
(async () => {
  const qs = (s) => document.querySelector(s);
  const pw = qs('input[type="password"]');
  let auth = null;
  try {
    const r = await fetch('/api/auth/me', { credentials: 'include' });
    let body = null;
    try { body = await r.json(); } catch (e) {}
    auth = { status: r.status, ok: r.ok, body };
  } catch (e) {
    auth = { error: String(e) };
  }
  return {
    url: location.href,
    title: document.title,
    loginShell: !!qs('.login-shell'),
    passwordAutoComplete: pw ? (pw.getAttribute('autoComplete') || '') : null,
    auth,
  };
})()
"""


@dataclass
class LoginState:
    state: str  # login | setup | authenticated | unknown
    logged_in: bool
    adapter_id: str
    url: str | None = None
    title: str | None = None
    markers: dict[str, Any] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get_json(url: str, timeout: float = 5) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — loopback TestApe FS
        return json.loads(resp.read().decode("utf-8"))


def _assert_loopback(url: str) -> None:
    """Reject any navigation target that is not a loopback origin.

    The detector can drive the local TestApe browser to a URL; confine that to
    localhost so the endpoint cannot be used to point the controlled browser at
    an arbitrary external origin.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        raise ValueError(f"refusing non-loopback navigation target: {host or '(none)'}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"refusing non-http(s) navigation target: {parsed.scheme}")


def list_web_adapters(fs_url: str = FS_DEFAULT) -> list[tuple[str, str]]:
    """Return [(adapter_id, name)] for connected browser adapters known to TestApe."""
    docs = _get_json(f"{fs_url}/devices").get("docs", [])
    out: list[tuple[str, str]] = []
    for doc in docs:
        data = doc.get("data", {})
        if not data.get("connected") or not data.get("screenUrl"):
            continue
        haystack = " ".join(
            str(data.get(k) or "").lower()
            for k in ("name", "customName", "type", "adapter_type", "device-brand")
        )
        if not any(hint in haystack for hint in _BROWSER_HINTS):
            continue
        adapter_id = doc.get("id", "")
        out.append((adapter_id, data.get("name") or adapter_id))
    return out


@contextmanager
def _web_session(adapter_id: str, fs_url: str) -> Iterator[Any]:
    """Open a TestApe WebSession on an adapter; close it on exit."""
    os.environ.setdefault("TESTAPE_local_mode", "1")
    from testape_engine.engine import Engine
    from testape_engine.web_session import WebSession

    user_id = os.environ.get("TESTAPE_USER_ID", "default")
    customer_id = os.environ.get("TESTAPE_CUSTOMER_ID", "default")
    engine = Engine.from_local(user_id, customer_id, base_url=fs_url)
    session = engine.start_session(adapter_id)
    try:
        yield WebSession(session)
    finally:
        try:
            session.close()
        except Exception:
            logger.exception("error closing testape session for adapter %s", adapter_id)


def _coerce_markers(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _classify(adapter_id: str, markers: dict[str, Any]) -> LoginState:
    url = markers.get("url")
    title = markers.get("title")
    auth = markers.get("auth") or {}

    # DOM takes priority: if a login/setup form is rendered, that is what the
    # user sees, regardless of session state. (In the real app the two agree —
    # a login screen implies a 401 — but the rendered form is the ground truth
    # for a login detector.)
    if markers.get("loginShell"):
        pw = str(markers.get("passwordAutoComplete") or "").lower()
        state = "setup" if pw == "new-password" else "login"
        return LoginState(state, False, adapter_id, url, title, markers)

    if auth.get("ok"):  # /api/auth/me => 200
        return LoginState("authenticated", True, adapter_id, url, title, markers)

    if auth.get("status") is None:
        reason = "app unreachable (auth endpoint did not respond)"
    else:
        reason = f"not logged in (auth/me {auth.get('status')}) and no login screen rendered"
    return LoginState("unknown", False, adapter_id, url, title, markers, reason=reason)


def detect_login_state(
    adapter_id: str | None = None,
    url: str | None = None,
    fs_url: str = FS_DEFAULT,
) -> LoginState:
    """Detect the login state of the app open in a TestApe web adapter.

    adapter_id: target adapter; if omitted, the first connected browser adapter is used.
    url: if given (loopback only), the adapter navigates there before detecting;
         otherwise the adapter's current page is inspected.
    """
    if not adapter_id:
        adapters = list_web_adapters(fs_url)
        if not adapters:
            return LoginState(
                "unknown", False, "", reason="no connected TestApe web adapter"
            )
        adapter_id = adapters[0][0]

    if url:
        _assert_loopback(url)

    with _web_session(adapter_id, fs_url) as web:
        if url:
            web.navigate(url)
        raw = web.eval_js(_DETECT_JS)

    return _classify(adapter_id, _coerce_markers(raw))

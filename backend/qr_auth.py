"""One-time QR login grants + rotating refresh tokens for external access.

Three token kinds, by lifetime and trust:

  - grant   — opaque random, server-side, SINGLE USE, 5 min. Encoded in
              the login-screen QR. Possession ⇒ one redemption. Minted
              only from loopback / an authenticated session (see
              auth_routes.qr_grant); redeemed by anyone (the phone).
  - refresh — opaque random "<fam>.<jti>", server-side, ROTATING, with a
              *sliding* expiry (default 7 days, BA_REFRESH_TTL_DAYS). Each
              use mints a new jti, invalidates the old one, and pushes the
              expiry out again — so a phone that opens the app within the
              window stays logged in, while a token unused past it is
              dropped. A replayed (already-rotated) jti is treated as theft
              and revokes the whole family. Never leaves the client except
              to /api/auth/refresh.
  - access  — SIGNED + stateless (auth.create_access_token / verify_token),
              15 min. This is what the auth gate checks on every request,
              so it stays a stateless verify with no per-request lookup.

State lives in $BA_HOME/qr_auth_state.json (0600), guarded by a process
lock. ponytail: single-worker assumption (run.sh runs one uvicorn); move
to sqlite/redis if we ever fan out to multiple workers.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from threading import Lock
from typing import Any

import auth
from paths import atomic_replace, ba_home

GRANT_TTL = 5 * 60          # seconds — QR is short-lived on purpose
# Sliding window: each refresh pushes the expiry out again, so a phone
# that opens the app within the window stays signed in; one untouched for
# the whole window is dropped and re-onboards via QR. Tune via
# BA_REFRESH_TTL_DAYS.
REFRESH_TTL = int(os.environ.get("BA_REFRESH_TTL_DAYS") or "7") * 86400
_FILE_MODE = 0o600
_lock = Lock()


def _path():
    return ba_home() / "qr_auth_state.json"


def _now() -> float:
    return time.time()


def _read() -> dict[str, Any]:
    try:
        st = _path().stat()
        if st.st_mode & 0o077:  # refuse to trust a world/group-readable file
            return {"grants": {}, "families": {}}
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {"grants": {}, "families": {}}
    if not isinstance(data, dict):
        return {"grants": {}, "families": {}}
    data.setdefault("grants", {})
    data.setdefault("families", {})
    return data


def _prune(state: dict[str, Any]) -> None:
    now = _now()
    state["grants"] = {k: v for k, v in state["grants"].items() if v > now}
    state["families"] = {
        k: v for k, v in state["families"].items() if float(v.get("exp", 0)) > now
    }


def _write(state: dict[str, Any]) -> None:
    _prune(state)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(state, h, separators=(",", ":"))
            h.flush()
            os.fsync(h.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    atomic_replace(tmp, path)
    os.chmod(path, _FILE_MODE)


# --- one-time grants -------------------------------------------------

def mint_grant() -> str:
    """Create a single-use grant and return its opaque token."""
    token = secrets.token_urlsafe(32)
    with _lock:
        state = _read()
        state["grants"][token] = _now() + GRANT_TTL
        _write(state)
    return token


def consume_grant(candidate: str | None) -> bool:
    """Redeem a grant exactly once. False if unknown / expired / reused."""
    token = str(candidate or "").strip()
    if not token:
        return False
    with _lock:
        state = _read()
        exp = state["grants"].pop(token, None)
        if exp is None:
            # Unknown grant: nothing changed, so DON'T write. Writing here
            # fsync'd the whole state file on every bogus redeem — a public,
            # unauthenticated, event-loop-blocking DoS amplifier.
            return False
        ok = float(exp) > _now()
        _write(state)
        return ok


# --- rotating refresh tokens ----------------------------------------

def issue_session(sub: str) -> tuple[str, str]:
    """Start a refresh family for `sub`. Returns (access, refresh)."""
    fam = secrets.token_urlsafe(18)
    jti = secrets.token_urlsafe(18)
    with _lock:
        state = _read()
        state["families"][fam] = {"jti": jti, "sub": sub, "exp": _now() + REFRESH_TTL}
        _write(state)
    return auth.create_access_token(sub), f"{fam}.{jti}"


def rotate(refresh_token: str | None) -> tuple[str, str] | None:
    """Verify + rotate a refresh token. Returns (access, new_refresh), or
    None if invalid/expired. Replaying an already-rotated token is theft:
    the whole family is revoked so the legitimate holder's next refresh
    fails too and re-onboarding via QR is forced."""
    raw = str(refresh_token or "").strip()
    fam, _, jti = raw.partition(".")
    if not fam or not jti:
        return None
    with _lock:
        state = _read()
        rec = state["families"].get(fam)
        if not rec:
            # Unknown family (the cheap, attacker-controllable case — any
            # garbage token): nothing changed, so DON'T write/fsync. Avoids
            # the public DoS amplifier. Reaching the expired/mismatch paths
            # below requires guessing a live 144-bit family id, so writing
            # there is not a cheap-attack vector.
            return None
        if float(rec.get("exp", 0)) <= _now():
            state["families"].pop(fam, None)
            _write(state)
            return None
        if not secrets.compare_digest(str(rec.get("jti", "")), jti):
            # Reuse of a superseded token → revoke the family.
            state["families"].pop(fam, None)
            _write(state)
            return None
        new_jti = secrets.token_urlsafe(18)
        sub = str(rec["sub"])
        rec["jti"] = new_jti
        rec["exp"] = _now() + REFRESH_TTL  # sliding expiry on active use
        _write(state)
    return auth.create_access_token(sub), f"{fam}.{new_jti}"

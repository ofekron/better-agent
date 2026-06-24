"""Password verification + login rate limiting.

The username/hash are read once at startup from the macOS keychain
via `auth_secrets`; comparing the provided password against the
stored argon2id hash is constant-time by argon2's design.

Rate limit: simple in-memory deque per source IP. Tuned for a
single-user box exposed to LAN — assumes a single uvicorn worker
(see run.sh). If we ever scale to multiple workers, swap for a
shared store (sqlite / redis).
"""

import asyncio
import ipaddress
import os
import time
from collections import defaultdict, deque
from threading import Lock

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

import secrets as _secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import auth_secrets

_ph = PasswordHasher()

# Cache the username/hash at module import — they don't change at
# runtime (rotated only via `./run.sh --reset-auth` which restarts
# the backend). Avoids a `security` shell-out on every login attempt.
#
# ``read_all_parallel`` fetches all three keychain entries concurrently
# (ThreadPoolExecutor) so three 5 s timeouts collapse into one.
#
# BOOTSTRAP MODE: on a fresh install no credentials exist yet. The macOS
# path (run.sh / desktop setup.py) guarantees they're written before the
# app imports, but the cross-platform web path can't. So tolerate their
# absence here: boot credential-less, serve only the first-run setup
# screen (see /api/auth/setup + /api/auth/needs_setup in auth_routes.py),
# and `reload_credentials()` once the user submits them.

# Ephemeral secret so SessionMiddleware always has *something* to sign
# with during bootstrap. Sessions created pre-bootstrap don't survive a
# restart (the real keychain secret loads then) — acceptable, since the
# only pre-bootstrap session is the one minted the moment setup succeeds.
_EPHEMERAL_SECRET = _secrets.token_hex(32)


def _load() -> tuple:
    """Read the three credential entries; return (user, hash, secret,
    bootstrapped). On a fresh install the read raises (missing entry) —
    swallow it and report not-bootstrapped instead of crashing import."""
    try:
        u, h, s = auth_secrets.read_all_parallel()
        return u, h, s, True
    except Exception:
        return None, None, None, False


_USERNAME, _PASSWORD_HASH, SESSION_SECRET, _BOOTSTRAPPED = _load()


def is_bootstrapped() -> bool:
    """True once credentials exist. False on a fresh install, when only
    the first-run setup endpoint should be reachable."""
    return _BOOTSTRAPPED


def reload_credentials() -> None:
    """Re-read credentials after first-run setup writes them, so the
    running process flips to bootstrapped without a restart."""
    global _USERNAME, _PASSWORD_HASH, SESSION_SECRET, _BOOTSTRAPPED
    _USERNAME, _PASSWORD_HASH, SESSION_SECRET, _BOOTSTRAPPED = _load()


def get_session_secret() -> str:
    return SESSION_SECRET if _BOOTSTRAPPED else _EPHEMERAL_SECRET


def is_test_auth_bypass_request(request) -> bool:
    if os.environ.get("BETTER_CLAUDE_TEST_AUTH_BYPASS") != "1":
        return False
    client = request.client
    host = client.host if client else None
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


# ---------------------------------------------------------------------
# Bearer tokens — for native (Capacitor) clients that can't rely on
# cross-origin session cookies. The Capacitor WebView runs at
# http://localhost/ but the backend lives at http://<lan-ip>:8000;
# SameSite=Lax drops the better_agent_session cookie on every cross-origin
# fetch after the login response, so the user appears to "sign in"
# but immediately drops back to <Login />. Bearer-in-header bypasses
# that entirely.
#
# Same signing key + namespace as SessionMiddleware so issuing /
# rotating credentials still kills outstanding tokens (each `secret`
# rotation invalidates everything signed with the old one).
_TOKEN_NAMESPACE = "bc-bearer-v1"
_TOKEN_MAX_AGE = 30 * 86400  # 30 days, mirrors SessionMiddleware


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_session_secret(), salt=_TOKEN_NAMESPACE)


def create_token(username: str) -> str:
    """Sign + return a bearer token carrying the username. Counterpart
    of the cookie session payload. Token is opaque to the client."""
    return _serializer().dumps({"username": username})


def verify_token(token: str | None) -> dict | None:
    """Decode + return the embedded user dict, or None if the token
    is missing / malformed / expired / signed by a stale secret."""
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or "username" not in payload:
        return None
    return {"username": payload["username"]}


# Constant-time string equality. `argon2.verify` already does this for
# the password; we use the same property here for the username so a
# bad username can't be distinguished from a bad password via timing.
def _const_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        # Still walk the longer string to make even length-mismatch
        # take constant time. Cheap because usernames are short.
        a, b = a.ljust(64, "\0"), b.ljust(64, "\0")
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


async def verify_credentials(username: str, password: str) -> bool:
    """Returns True iff (username, password) matches the configured
    user. Always pays the full argon2 verify cost — even on a wrong
    username — so a non-allowlisted username can't be enumerated via
    timing. 200ms tarpit on every failure (success path is fast)."""
    if not _BOOTSTRAPPED:
        # No credentials configured yet — nothing can match. The setup
        # screen, not login, is the correct entry point in this state.
        await asyncio.sleep(0.2)
        return False
    user_ok = _const_eq(username, _USERNAME)
    try:
        _ph.verify(_PASSWORD_HASH, password)
        pw_ok = True
    except (VerifyMismatchError, InvalidHashError):
        pw_ok = False
    if user_ok and pw_ok:
        return True
    # Tarpit. Run in the asyncio loop so the event loop is happy.
    await asyncio.sleep(0.2)
    return False


# ---------------------------------------------------------------------
# Rate limiter — per source IP, sliding window.
# ---------------------------------------------------------------------

_RL_WINDOW = 300.0   # seconds
_RL_MAX = 5          # attempts per window before lock-out
_rl_attempts: dict[str, deque[float]] = defaultdict(deque)
_rl_lock = Lock()
# Monotonic ts of the last stale-entry prune. The table is swept at most
# once per `_RL_WINDOW`, so it stays bounded by the number of distinct IPs
# seen within one window. Without this, every distinct source IP that hits
# the login endpoint without succeeding leaks an entry forever — a pre-auth
# memory-DoS, trivially reachable over IPv6 where one host owns a vast
# address range (`request.client.host` differs per address).
_rl_last_sweep = 0.0


def _rl_prune_stale(now: float) -> None:
    """Drop IP entries whose most recent attempt has aged out of the
    window. Attempts are appended in time order, so `dq[-1] < cutoff`
    means the whole entry is stale. Caller MUST hold `_rl_lock`."""
    cutoff = now - _RL_WINDOW
    for ip in [k for k, dq in _rl_attempts.items() if not dq or dq[-1] < cutoff]:
        del _rl_attempts[ip]


def rate_limit_check(ip: str) -> bool:
    """Returns True if the request is within budget; False if the IP
    has exceeded `_RL_MAX` failed attempts in the last `_RL_WINDOW`
    seconds. Call BEFORE attempting verification — the limiter
    counts attempts, not failures, so a malicious client can't burn
    through the budget faster by sending invalid credentials."""
    global _rl_last_sweep
    now = time.monotonic()
    cutoff = now - _RL_WINDOW
    with _rl_lock:
        if now - _rl_last_sweep >= _RL_WINDOW:
            _rl_prune_stale(now)
            _rl_last_sweep = now
        dq = _rl_attempts[ip]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _RL_MAX:
            return False
        dq.append(now)
        return True


def rate_limit_reset(ip: str) -> None:
    """Clear an IP's attempt history. Called on successful login so
    a legitimate user who fat-fingered their password 4 times isn't
    one typo away from a 5-minute lock-out."""
    with _rl_lock:
        _rl_attempts.pop(ip, None)

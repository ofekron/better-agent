"""Read auth credentials from the user's macOS login keychain.

INVARIANT — MUST shell out to /usr/bin/security; do NOT switch to the
python `keyring` package (which IS in the venv, version 25.7.0).

The macOS keychain ACL is bound to the caller binary. `security` is
whitelisted to read its own entries (no GUI prompt). Switching this
module to `keyring` makes the python interpreter the caller, which
flips the ACL and triggers a GUI permission prompt on every backend
start — including non-interactive starts from `run.sh` where there's
no one to click "Always Allow".

run.sh writes these entries on first run; this module reads them.
Three accounts live under the `better-agent` service, with legacy
`better-claude` fallback:
  - username        — the chosen login name
  - password_hash   — argon2id hash of the password
  - session_secret  — 32-byte hex used to sign session cookies

Calls fail loud (RuntimeError) if any entry is missing or the
keychain is locked. The intended remediation is shown in the
exception message.
"""

import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
import sys

import oskeychain
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE, auth_services, service_names

_SERVICE = PRIMARY_SERVICE
_LEGACY_SERVICE = LEGACY_SERVICE
_KC_TIMEOUT = 5  # seconds — fail loud rather than hang on a locked keychain


def _services() -> tuple[str, ...]:
    # Home-scoped: the default home uses the shared "better-agent" entries
    # (backward compat); any other BETTER_AGENT_HOME gets its own suffixed
    # auth store so each Better Agent instance owns its own user/password.
    return auth_services()


def _kc_get(service: str, account: str) -> str:
    if sys.platform != "darwin":
        import keyring
        val = keyring.get_password(service, account)
        if val is None:
            raise RuntimeError(
                f"Credential entry missing: service={service} account={account}. "
                f"Run the bootstrap (set credentials) before starting the backend."
            )
        return val.strip()
    try:
        out = subprocess.check_output(
            ["/usr/bin/security", "find-generic-password",
             "-s", service, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_KC_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Keychain locked or unresponsive. Unlock Keychain Access "
            "(or run `security unlock-keychain login.keychain-db`) and "
            "restart the backend."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Keychain entry missing: service={service} account={account}. "
            f"Run ./run.sh to bootstrap credentials, or ./run.sh --reset-auth "
            f"to wipe and re-prompt."
        ) from exc
    return out.strip()


def _kc(account: str) -> str:
    """Read one keychain entry by account name. Raises on missing /
    locked / non-macOS. Returns the stored value verbatim (trailing
    newline stripped — `security -w` doesn't add one, but `.strip()`
    defends against an admin accidentally pasting a value with
    whitespace via a UI tool)."""
    errors: list[RuntimeError] = []
    for service in _services():
        try:
            return _kc_get(service, account)
        except RuntimeError as exc:
            errors.append(exc)
    raise errors[-1]


def get_username() -> str:
    return _kc("username")


def get_password_hash() -> str:
    return _kc("password_hash")


def get_session_secret() -> str:
    return _kc("session_secret")


def read_all_parallel() -> tuple[str, str, str]:
    """Read all three auth secrets in parallel via ThreadPoolExecutor.

    Each ``/usr/bin/security`` call takes up to ``_KC_TIMEOUT`` (5 s) on a
    locked keychain. Reading them sequentially means 3 × timeout in the
    worst case; reading them in parallel caps it at 1 × timeout.
    """
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="warm-auth") as pool:
        f_user = pool.submit(get_username)
        f_hash = pool.submit(get_password_hash)
        f_secret = pool.submit(get_session_secret)
    return f_user.result(), f_hash.result(), f_secret.result()


# ── First-run bootstrap (write side) ─────────────────────────────────
# `run.sh` writes these entries on a dev checkout; the desktop app's
# first-run setup writes them via `write_credentials`. Both go through
# `/usr/bin/security` so `security` owns each entry's ACL — reads then
# need no GUI prompt (see the module docstring).


def _account_exists(account: str) -> bool:
    """True if the keychain holds this account under a Better Agent
    service. Probes attributes only (no `-g`/`-w`) so it never reads a
    value — no GUI prompt, no darwin-only read path."""
    for service in _services():
        if sys.platform != "darwin":
            import keyring
            if keyring.get_password(service, account) is not None:
                return True
            continue
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", service, "-a", account],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_KC_TIMEOUT,
        )
        if result.returncode == 0:
            return True
    return False


def needs_bootstrap() -> bool:
    """True when any of the three credential entries is missing — the
    desktop app must run first-run setup before starting the backend."""
    return not all(
        _account_exists(a)
        for a in ("username", "password_hash", "session_secret")
    )


def _kc_set(account: str, value: str) -> None:
    """Write (replacing) one keychain entry via the shared platform helper."""
    for service in _services():
        oskeychain.store(service, account, value)


def make_password_hash(password: str) -> str:
    """argon2id hash of the login password — the format `auth.py` verifies."""
    import argon2
    return argon2.PasswordHasher().hash(password)


def write_credentials(username: str, password: str) -> None:
    """First-run bootstrap: store username, the argon2 password hash, and
    a freshly minted 32-byte session secret in the keychain."""
    if not username or not password:
        raise ValueError("username and password must both be non-empty")
    _kc_set("username", username)
    _kc_set("password_hash", make_password_hash(password))
    _kc_set("session_secret", secrets.token_hex(32))


def write_login_credentials(username: str, password: str) -> None:
    if not username or not password:
        raise ValueError("username and password must both be non-empty")
    _kc_set("username", username)
    _kc_set("password_hash", make_password_hash(password))

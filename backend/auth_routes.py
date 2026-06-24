"""Login / logout / whoami HTTP routes.

Gated externally by the auth_gate middleware in main.py — these
routes are explicitly exempted there because logging in is
necessarily the only way to acquire a session.
"""

import ipaddress

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

import auth
import auth_secrets
import setup_nonce

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str
    password: str


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@router.get("/needs_setup")
async def needs_setup() -> dict:
    """True on a fresh install with no credentials yet — the frontend
    renders the first-run <Setup /> screen instead of <Login />. This is
    the cross-platform equivalent of run.sh's terminal prompt and the
    desktop app's native setup dialog (desktop/setup.py)."""
    return {"needs_setup": not auth.is_bootstrapped()}


@router.post("/setup")
async def setup(body: SetupBody, request: Request) -> dict:
    """First-run credential bootstrap. Allowed ONLY while no credentials
    exist (409 otherwise — this is not a password-change endpoint). Writes
    the credentials via the same `auth_secrets.write_credentials` the
    desktop setup uses, reloads them into the running process, and logs
    the user in immediately so they land in the app without a round-trip
    through the login screen.

    Returns a bearer token in the body so native clients (Capacitor)
    can authenticate without relying on the cross-origin session cookie."""
    if auth.is_bootstrapped():
        raise HTTPException(status_code=409, detail="already configured")
    if not _is_loopback_request(request):
        nonce = request.headers.get("X-Setup-Nonce")
        if not setup_nonce.consume(nonce):
            raise HTTPException(status_code=403, detail="setup requires loopback or setup nonce")
    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(status_code=400, detail="username and password required")
    try:
        auth_secrets.write_credentials(username, body.password)
    except Exception as exc:  # noqa: BLE001 — surface the failure to the UI
        raise HTTPException(status_code=500, detail=f"could not save credentials: {exc}")
    auth.reload_credentials()
    request.session["user"] = {"username": username}
    return {"token": auth.create_token(username)}


@router.post("/login")
async def login(body: LoginBody, request: Request) -> dict:
    """Verify credentials, set the session cookie + return a bearer token.

    Same 401 status for bad-username vs bad-password (no enumeration).
    429 when the source IP has exhausted its attempt budget.

    Browsers/desktop use the session cookie; native clients (Capacitor)
    use the bearer token because the cookie can't survive the
    cross-origin hop from the WebView to the backend.
    """
    ip = request.client.host if request.client else "unknown"
    if not auth.rate_limit_check(ip):
        # Don't tarpit the 429 — the limiter itself is the back-pressure.
        raise HTTPException(status_code=429, detail="too many attempts")
    if not await auth.verify_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    # Success — clear the IP's failure record and stamp the session.
    auth.rate_limit_reset(ip)
    request.session["user"] = {"username": body.username}
    return {"token": auth.create_token(body.username)}


@router.post("/logout", status_code=204)
async def logout(request: Request) -> None:
    """Clear the session cookie. Stateless cookie design means a
    pre-logout stolen cookie remains valid until its 30-day expiry —
    documented limitation."""
    request.session.clear()


@router.get("/me")
async def me(request: Request) -> dict:
    """Returns the logged-in user or 401. Used by the frontend at
    mount time to decide whether to render <Login /> or <AppMain />."""
    if auth.is_test_auth_bypass_request(request):
        return {"username": "test-auth-bypass"}
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return user

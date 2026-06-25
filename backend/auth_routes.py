"""Login / logout / whoami HTTP routes.

Gated externally by the auth_gate middleware in main.py — these
routes are explicitly exempted there because logging in is
necessarily the only way to acquire a session.
"""

import ipaddress
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

import auth
import auth_secrets
import qr_auth
import setup_nonce

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str
    password: str


class ChangeCredentialsBody(BaseModel):
    current_username: str
    current_password: str
    new_username: str
    new_password: str


class RedeemBody(BaseModel):
    grant: str


class RefreshBody(BaseModel):
    refresh_token: str


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _is_authenticated(request: Request) -> bool:
    """Cookie session OR a valid bearer. qr_grant is in the public-route
    allowlist (so the loopback login screen can reach it), which means the
    gate never promotes a bearer into the session here — check it directly."""
    if request.session.get("user"):
        return True
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return auth.verify_token(header.split(" ", 1)[1].strip()) is not None
    return False


_FORWARD_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
)


def _has_forwarding_headers(request: Request) -> bool:
    """True if any reverse-proxy forwarding header is present. A same-host
    proxy connects from 127.0.0.1, so the loopback peer signal can no longer
    prove "physically at the machine" — unauthenticated QR minting must not
    trust loopback when forwarding is in play."""
    return any(h in request.headers for h in _FORWARD_HEADERS)


def _origin_host_port(raw: str) -> str | None:
    """host[:port] of an Origin/Referer URL, lowercased; None if unparseable."""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    return f"{host}:{parsed.port}" if parsed.port is not None else host


def _is_same_origin_browser_request(request: Request) -> bool:
    """True when a browser request demonstrably comes from the page served
    at THIS same origin, or carries no browser-origin signal at all (a
    non-browser loopback caller — curl, the desktop app). False for a
    CROSS-site fetch, e.g. a localhost:3000 dev server or local XSS calling
    the :8000 backend — the vector that lets arbitrary same-machine web
    content mint a QR grant off the shared loopback peer.

    `Sec-Fetch-Site` is the primary signal: the browser sets it and JS can't
    forge it. Origin/Referer-vs-Host is the fallback for clients that omit
    it."""
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None:
        return sfs in ("same-origin", "none")
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True  # no browser-origin signal → non-browser caller
    src = _origin_host_port(source)
    host = (request.headers.get("host") or request.url.netloc or "").lower()
    return src is not None and src == host


def _qr_mint_allowed(
    *, authed: bool, loopback: bool, has_forward: bool, same_origin: bool
) -> bool:
    """Gate for minting a one-time QR grant. An authenticated session may
    always mint. Otherwise (the on-machine admin at the login screen) minting
    is allowed ONLY from a loopback peer, for a same-origin / non-browser
    request, and only when no proxy-forwarding header collapsed the peer to
    loopback."""
    if authed:
        return True
    return loopback and same_origin and not has_forward


def _origin_base_url(request: Request) -> str:
    """The origin the *client* used to reach us, honoring a reverse proxy.
    The QR must point a phone back at the same externally-reachable origin
    the admin is viewing — not the backend's internal bind address."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}"


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


@router.post("/change_credentials")
async def change_credentials(body: ChangeCredentialsBody, request: Request) -> dict:
    if not request.session.get("user"):
        raise HTTPException(status_code=401, detail="unauthenticated")
    ip = request.client.host if request.client else "unknown"
    if not auth.rate_limit_check(ip):
        raise HTTPException(status_code=429, detail="too many attempts")
    if not await auth.verify_credentials(body.current_username, body.current_password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    new_username = body.new_username.strip()
    if not new_username or not body.new_password:
        raise HTTPException(status_code=400, detail="new username and password required")
    try:
        auth_secrets.write_login_credentials(new_username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not save credentials: {exc}") from exc
    auth.reload_credentials()
    auth.rate_limit_reset(ip)
    request.session["user"] = {"username": new_username}
    return {"username": new_username, "token": auth.create_token(new_username)}


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
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return user


# --- QR / refresh-token external access ------------------------------
# qr_grant mints a single-use QR (loopback or an authenticated admin
# only); a phone redeems it for a short access token + rotating refresh
# token. See qr_auth.py for the token model. All three are exempted in
# main.py's _AUTH_PUBLIC_ROUTES — qr_grant enforces its own gate below.


@router.get("/qr_grant")
async def qr_grant(request: Request) -> dict:
    """Mint a one-time login QR. Restricted to loopback or an already
    authenticated session so a stranger can't mint themselves an invite —
    only the admin at the machine (or a logged-in device) can. The phone
    that scans it redeems via /qr_redeem; nobody needs to type the
    password."""
    if not auth.is_bootstrapped():
        raise HTTPException(status_code=409, detail="not configured")
    if not _qr_mint_allowed(
        authed=_is_authenticated(request),
        loopback=_is_loopback_request(request),
        has_forward=_has_forwarding_headers(request),
        same_origin=_is_same_origin_browser_request(request),
    ):
        raise HTTPException(
            status_code=403,
            detail="qr grant requires loopback same-origin access or an authenticated session",
        )
    grant = qr_auth.mint_grant()
    login_url = f"{_origin_base_url(request)}/?qr={grant}"
    return {"login_url": login_url, "expires_in": qr_auth.GRANT_TTL}


@router.post("/qr_redeem")
async def qr_redeem(body: RedeemBody, request: Request) -> dict:
    """Redeem a one-time grant (from a scanned QR) for an access token +
    rotating refresh token. No cookie is set: external devices ride the
    short access token and refresh it, so there's no long-lived credential
    sitting on the phone."""
    ip = request.client.host if request.client else "unknown"
    if not auth.rate_limit_check(ip):
        raise HTTPException(status_code=429, detail="too many attempts")
    if not auth.is_bootstrapped():
        raise HTTPException(status_code=409, detail="not configured")
    if not qr_auth.consume_grant(body.grant):
        raise HTTPException(status_code=401, detail="invalid or used qr code")
    username = auth.current_username()
    if not username:
        raise HTTPException(status_code=409, detail="not configured")
    auth.rate_limit_reset(ip)
    access, refresh = qr_auth.issue_session(username)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": auth.ACCESS_MAX_AGE,
    }


@router.post("/refresh")
async def refresh(body: RefreshBody, request: Request) -> dict:
    """Rotate a refresh token: returns a fresh access + refresh pair and
    invalidates the one presented. A replayed (already-rotated) token
    revokes the whole family — see qr_auth.rotate."""
    ip = request.client.host if request.client else "unknown"
    if not auth.rate_limit_check(ip):
        raise HTTPException(status_code=429, detail="too many attempts")
    pair = qr_auth.rotate(body.refresh_token)
    if pair is None:
        raise HTTPException(status_code=401, detail="invalid or expired refresh token")
    auth.rate_limit_reset(ip)
    access, new_refresh = pair
    return {
        "access_token": access,
        "refresh_token": new_refresh,
        "expires_in": auth.ACCESS_MAX_AGE,
    }

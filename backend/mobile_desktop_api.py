"""Client-distribution surface: the installation capability profile, plus
the mobile (APK/IPA) and desktop (DMG/EXE + update repository) artifacts
staged under `ba_home()` and the phone/desktop-reachable base URL each
client needs to reach this server.

The global broadcast used to announce a capability change is injected by
the composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

import installation_capabilities
import installation_profile
from env_compat import get_env
from paths import ba_home

router = APIRouter()

_broadcast_global: Optional[Callable[..., Awaitable[Any]]] = None


def configure(broadcast_global: Callable[..., Awaitable[Any]]) -> None:
    """Bind the global broadcast this router announces changes through."""
    global _broadcast_global
    _broadcast_global = broadcast_global


MOBILE_DIR = ba_home() / "mobile"


def _desktop_downloads_dir() -> Path:
    return ba_home() / "desktop" / "downloads"


def _desktop_update_repo_dir() -> Path:
    return ba_home() / "desktop" / "updates" / "repository"


def _mobile_version() -> dict:
    """Read the staged build's version side-channel (written by the APK
    rebuild hook alongside the APK). Returns {} if absent so callers can
    treat version-checking as optional."""
    vf = MOBILE_DIR / "version.json"
    if not vf.exists():
        return {}
    try:
        with vf.open(encoding="utf-8") as f:
            data = json.load(f)
        code = data.get("version_code")
        return {
            "version_code": int(code) if isinstance(code, (int, float)) else None,
            "version_name": data.get("version_name"),
        }
    except (ValueError, OSError):
        return {}


def _desktop_version() -> dict:
    try:
        from _version import __version__ as version
    except ImportError:
        return {}
    return {"version": version}


def _lan_ip() -> str:
    """Best-effort primary LAN IPv4 of this machine, so the mobile QR
    encodes a phone-reachable address instead of localhost. Opens a UDP
    socket toward a public IP (no packets sent) and reads the local end
    the OS would route through. Falls back to 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _local_server_url(request: Request) -> str:
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    lan_ip = _lan_ip()
    return f"{request.url.scheme}://{lan_ip}:{port}"


def _preferred_server_url_info(request: Request, *, allow_loopback_https: bool = False) -> dict:
    import tailscale_https

    preference = tailscale_https.preferred_external_url_details(
        _local_server_url(request),
        allow_loopback_https=allow_loopback_https,
    )
    return {
        "server_url": preference.url,
        "server_url_source": preference.source,
        "https_available": preference.https_available,
        "https_unavailable_reason": preference.https_unavailable_reason,
    }


async def require_mobile_enabled() -> None:
    enabled = await asyncio.to_thread(installation_profile.mobile_enabled)
    if not enabled:
        raise HTTPException(status_code=404, detail="mobile support is not enabled")


@router.get("/api/installation-profile")
async def get_installation_profile():
    return await asyncio.to_thread(installation_profile.capabilities)


@router.patch("/api/installation-profile/capabilities/{capability}")
async def set_installation_capability(capability: str, body: dict | None = None):
    """Enable or disable a capability for this installation.

    The setting is user intent and takes effect for subsystems wired at the
    next start, so the response reports `restart_required` rather than
    claiming the change is already live. Disabling integrations cancels
    extension-owned work at that next start, so it needs explicit
    confirmation.
    """
    payload = body or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    if capability not in installation_capabilities.TOGGLEABLE:
        raise HTTPException(status_code=404, detail="unknown capability")
    if (
        capability == installation_capabilities.INTEGRATIONS
        and not enabled
        and payload.get("confirm_cancels_extension_work") is not True
    ):
        raise HTTPException(
            status_code=409,
            detail="disabling integrations cancels extension-owned work; confirm to proceed",
        )
    try:
        state = await asyncio.to_thread(
            installation_profile.set_capability_enabled, capability, enabled
        )
    except installation_profile.InstallationProfileError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if _broadcast_global is None:
        raise HTTPException(
            status_code=503, detail="mobile/desktop API is not configured"
        )
    await _broadcast_global("installation_capabilities_changed", state)
    return state


@router.get("/api/download/android")
async def download_android():
    """Serve the Android APK. Looks for any .apk file in ba_home()/mobile/."""
    await require_mobile_enabled()

    def _latest_apk() -> Path | None:
        MOBILE_DIR.mkdir(parents=True, exist_ok=True)
        apks = sorted(MOBILE_DIR.glob("*.apk"))
        return apks[-1] if apks else None

    apk = await asyncio.to_thread(_latest_apk)
    if apk is None:
        raise HTTPException(status_code=404, detail="No Android APK found. Place the APK in ~/.better-agent/mobile/")
    return FileResponse(
        apk,
        media_type="application/vnd.android.package-archive",
        filename=apk.name,
    )


@router.get("/api/download/ios")
async def download_ios():
    """Serve the iOS IPA. Looks for any .ipa file in ba_home()/mobile/."""
    await require_mobile_enabled()

    def _latest_ipa() -> Path | None:
        MOBILE_DIR.mkdir(parents=True, exist_ok=True)
        ipas = sorted(MOBILE_DIR.glob("*.ipa"))
        return ipas[-1] if ipas else None

    ipa = await asyncio.to_thread(_latest_ipa)
    if ipa is None:
        raise HTTPException(status_code=404, detail="No iOS IPA found. Place the IPA in ~/.better-agent/mobile/")
    return FileResponse(
        ipa,
        media_type="application/octet-stream",
        filename=ipa.name,
    )


@router.get("/api/mobile/status")
async def mobile_status(request: Request):
    """Return which mobile builds are available and the server's
    phone-reachable base URL (for QR code generation). Prefer verified
    Tailscale HTTPS when available; otherwise fall back to the LAN URL."""
    await require_mobile_enabled()

    def _mobile_build_status() -> dict:
        MOBILE_DIR.mkdir(parents=True, exist_ok=True)
        return {
            "android": any(MOBILE_DIR.glob("*.apk")),
            "ios": any(MOBILE_DIR.glob("*.ipa")),
            **_mobile_version(),
        }

    build_status = await asyncio.to_thread(_mobile_build_status)
    server_url_info = await asyncio.to_thread(_preferred_server_url_info, request)
    return {
        **server_url_info,
        **build_status,
    }


def _desktop_file_response(path: Path, filename: str | None = None):
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename or path.name)


def _desktop_update_file(rel_path: str) -> Path:
    root = _desktop_update_repo_dir().resolve()
    candidate = (root / rel_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise HTTPException(status_code=404, detail="desktop update file not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="desktop update file not found")
    return candidate


@router.get("/api/download/desktop/macos")
async def download_desktop_macos():
    def _dmg_path() -> Path | None:
        downloads_dir = _desktop_downloads_dir()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        dmg = downloads_dir / "BetterAgent.dmg"
        return dmg if dmg.exists() else None

    dmg = await asyncio.to_thread(_dmg_path)
    if dmg is None:
        raise HTTPException(status_code=404, detail="No macOS desktop build found")
    return _desktop_file_response(dmg)


@router.get("/api/download/desktop/windows")
async def download_desktop_windows():
    def _installer_path() -> Path | None:
        downloads_dir = _desktop_downloads_dir()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        installer = downloads_dir / "BetterAgentSetup.exe"
        return installer if installer.exists() else None

    installer = await asyncio.to_thread(_installer_path)
    if installer is None:
        raise HTTPException(status_code=404, detail="No Windows desktop build found")
    return _desktop_file_response(installer)


@router.get("/api/desktop/status")
async def desktop_status(request: Request):
    def _desktop_build_status() -> dict:
        downloads_dir = _desktop_downloads_dir()
        update_repo_dir = _desktop_update_repo_dir()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        update_repo_dir.mkdir(parents=True, exist_ok=True)
        return {
            "macos": (downloads_dir / "BetterAgent.dmg").exists(),
            "windows": (downloads_dir / "BetterAgentSetup.exe").exists(),
            "update_repo": (update_repo_dir / "metadata" / "root.json").exists(),
            **_desktop_version(),
        }

    build_status = await asyncio.to_thread(_desktop_build_status)
    server_url_info = await asyncio.to_thread(
        _preferred_server_url_info,
        request,
        allow_loopback_https=True,
    )
    server_url = server_url_info["server_url"]
    return {
        "desktop_shell": get_env("BETTER_CLAUDE_DESKTOP_SHELL") == "1",
        **server_url_info,
        "update_url": f"{server_url}/api/desktop/updates",
        **build_status,
    }


@router.get("/api/desktop/updates/{rel_path:path}")
async def desktop_update_file(rel_path: str):
    path = await asyncio.to_thread(_desktop_update_file, rel_path)
    return _desktop_file_response(path, filename=None)

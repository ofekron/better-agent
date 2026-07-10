from __future__ import annotations

import email.utils
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from paths import bc_home
from portable_lock import lock_ex, unlock

_VERSION = 1
_MIN_COOLDOWN_SECS = 1.0
_DEFAULT_COOLDOWN_SECS = 60.0
_MAX_COOLDOWN_SECS = 900.0
_LEASE_SECS = 30.0
_LOCAL_CACHE_MAX = 512
_local_cooldowns: dict[str, float] = {}
_local_lock = threading.Lock()


@dataclass(frozen=True)
class ProbeLease:
    scope: str
    token: str


@dataclass(frozen=True)
class Claim:
    lease: ProbeLease | None
    reason: str
    recovered: bool = False
    corrupt: bool = False


def _root() -> Path:
    path = bc_home() / "runtime" / "shortcut-rate-limit"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _salt() -> bytes:
    root = _root()
    path = root / "scope.key"
    lock_path = root / "scope.key.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(lock_fd, 0o600)
    try:
        lock_ex(lock_fd)
        try:
            value = path.read_bytes()
        except FileNotFoundError:
            value = b""
        if len(value) == 32:
            os.chmod(path, 0o600)
            return value
        value = secrets.token_bytes(32)
        fd, tmp_name = tempfile.mkstemp(prefix=".scope.key.", dir=root)
        tmp = Path(tmp_name)
        try:
            os.write(fd, value)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            _fsync_parent(path)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return value
    finally:
        unlock(lock_fd)
        os.close(lock_fd)


def _normalized_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    port = parsed.port
    authority = host if port in (None, default_port) else f"{host}:{port}"
    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else ""
    return f"{scheme}://{authority}{path}"


def scope_key(*, provider_id: str, base_url: str, model: str, api_key: str) -> str:
    credential = hmac.new(_salt(), api_key.encode("utf-8"), hashlib.sha256).hexdigest()
    payload = json.dumps(
        [_normalized_endpoint(base_url), provider_id, model, credential],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _paths(scope: str) -> tuple[Path, Path]:
    if len(scope) != 64 or any(char not in "0123456789abcdef" for char in scope):
        raise ValueError("shortcut rate-limit scope must be lowercase SHA-256 hex")
    root = _root()
    return root / f"{scope}.json", root / f"{scope}.lock"


def _read(path: Path) -> tuple[dict, bool]:
    if not path.exists():
        return {}, False
    try:
        os.chmod(path, 0o600)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, True
    if not isinstance(value, dict) or value.get("version") != _VERSION:
        return {}, True
    numeric = ("observed_epoch", "cooldown_until_epoch", "lease_until_epoch")
    if any(not isinstance(value.get(key, 0), (int, float)) for key in numeric):
        return {}, True
    if not isinstance(value.get("lease_token", ""), str):
        return {}, True
    return value, False


def _write(path: Path, value: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_parent(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _fsync_parent(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path.parent, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        if os.name != "nt":
            raise


def _clock_state(state: dict, now: float) -> tuple[dict, float, bool]:
    observed = float(state.get("observed_epoch", 0))
    if observed <= now:
        return state, now, False
    shift = observed - now
    rebased = dict(state)
    rebased["observed_epoch"] = now
    for key in ("cooldown_until_epoch", "lease_until_epoch"):
        rebased[key] = max(0.0, float(rebased.get(key, 0)) - shift)
    return rebased, now, True


def _local_cooldown_active(scope: str) -> bool:
    current = time.monotonic()
    with _local_lock:
        deadline = _local_cooldowns.get(scope, 0)
        if deadline > current:
            return True
        _local_cooldowns.pop(scope, None)
    return False


def _set_local_cooldown(scope: str, seconds: float) -> None:
    with _local_lock:
        if seconds <= 0:
            _local_cooldowns.pop(scope, None)
            return
        _local_cooldowns[scope] = time.monotonic() + min(seconds, _MAX_COOLDOWN_SECS)
        while len(_local_cooldowns) > _LOCAL_CACHE_MAX:
            _local_cooldowns.pop(next(iter(_local_cooldowns)))


def _locked(scope: str, action):
    state_path, lock_path = _paths(scope)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    try:
        lock_ex(fd)
        state, corrupt = _read(state_path)
        return action(state_path, state, corrupt)
    finally:
        unlock(fd)
        os.close(fd)


def claim(scope: str, *, now: float | None = None) -> Claim:
    if now is None and _local_cooldown_active(scope):
        return Claim(None, "cooldown")
    wall_now = time.time() if now is None else now

    def apply(path: Path, state: dict, corrupt: bool) -> Claim:
        state, current, rebased = _clock_state(state, wall_now)
        if rebased:
            _write(path, state)
        remaining = float(state.get("cooldown_until_epoch", 0)) - current
        if remaining > 0:
            _set_local_cooldown(scope, remaining)
            return Claim(None, "cooldown", corrupt=corrupt)
        lease_until = float(state.get("lease_until_epoch", 0))
        if lease_until > current:
            return Claim(None, "inflight", corrupt=corrupt)
        recovered = bool(state.get("lease_token"))
        token = uuid.uuid4().hex
        _write(path, {
            "version": _VERSION,
            "observed_epoch": current,
            "cooldown_until_epoch": 0,
            "lease_token": token,
            "lease_until_epoch": current + _LEASE_SECS,
        })
        return Claim(ProbeLease(scope, token), "probe", recovered, corrupt)

    return _locked(scope, apply)


def finish(
    lease: ProbeLease,
    *,
    cooldown_secs: float | None = None,
    now: float | None = None,
) -> bool:
    wall_now = time.time() if now is None else now

    def apply(path: Path, state: dict, _corrupt: bool) -> bool:
        state, current, _rebased = _clock_state(state, wall_now)
        if state.get("lease_token") != lease.token:
            return False
        cooldown = 0.0 if cooldown_secs is None else max(
            _MIN_COOLDOWN_SECS,
            min(float(cooldown_secs), _MAX_COOLDOWN_SECS),
        )
        _write(path, {
            "version": _VERSION,
            "observed_epoch": current,
            "cooldown_until_epoch": current + cooldown,
            "lease_token": "",
            "lease_until_epoch": 0,
        })
        return True

    completed = _locked(lease.scope, apply)
    if completed:
        _set_local_cooldown(lease.scope, 0 if cooldown_secs is None else max(
            _MIN_COOLDOWN_SECS,
            min(float(cooldown_secs), _MAX_COOLDOWN_SECS),
        ))
    return completed


def retry_after_seconds(
    value: str | None,
    *,
    response_date: str | None = None,
    now: float | None = None,
) -> float:
    if value:
        try:
            seconds = float(value.strip())
        except ValueError:
            try:
                target = email.utils.parsedate_to_datetime(value).timestamp()
                anchor = time.time() if now is None else now
                if response_date:
                    try:
                        anchor = email.utils.parsedate_to_datetime(response_date).timestamp()
                    except (TypeError, ValueError, OverflowError):
                        pass
                seconds = target - anchor
            except (TypeError, ValueError, OverflowError):
                seconds = _DEFAULT_COOLDOWN_SECS
        if seconds >= 0:
            return max(_MIN_COOLDOWN_SECS, min(seconds, _MAX_COOLDOWN_SECS))
    return _DEFAULT_COOLDOWN_SECS

from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
import struct
import tempfile
import threading
import time
import urllib.parse
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .control import state
from .paths import pointer_path, web_access_path
from .requests import submit
from .web_ui import HTML, MANIFEST, SERVICE_WORKER

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 18768
_MAX_BODY = 1024
_AUTH_WINDOW_SECONDS = 60
_AUTH_FAILURE_LIMIT = 12


def _png_icon(size: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    pixel = bytes((17, 23, 37, 255))
    rows = b"".join(b"\0" + pixel * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def _access_config() -> dict[str, Any]:
    path = web_access_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    token = raw.get("token") if isinstance(raw.get("token"), str) else ""
    if len(token) < 43:
        token = secrets.token_urlsafe(32)
    config = {"token": token, "host": DEFAULT_HOST, "port": DEFAULT_PORT}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".switch-web-", dir=path.parent)
    temporary = os.fdopen(descriptor, "w", encoding="utf-8")
    try:
        os.fchmod(temporary.fileno(), stat.S_IRUSR | stat.S_IWUSR)
        json.dump(config, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
    finally:
        temporary.close()
    try:
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return config


def _running_checkout() -> str:
    try:
        payload = json.loads(pointer_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        payload = {}
    active = payload.get("active")
    if not isinstance(active, str) or not active:
        raise RuntimeError("active checkout is unavailable")
    return active


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], token: str):
        super().__init__(address, _Handler)
        self.token = token
        self.auth_failures: dict[str, list[float]] = {}
        self.auth_lock = threading.Lock()

    def auth_limited(self, client: str) -> bool:
        now = time.monotonic()
        with self.auth_lock:
            recent = [item for item in self.auth_failures.get(client, []) if now - item < _AUTH_WINDOW_SECONDS]
            self.auth_failures[client] = recent
            return len(recent) >= _AUTH_FAILURE_LIMIT

    def record_auth_failure(self, client: str) -> None:
        with self.auth_lock:
            self.auth_failures.setdefault(client, []).append(time.monotonic())


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _allowed_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        if not origin:
            return ""
        try:
            parsed = urllib.parse.urlsplit(origin)
            request_host = urllib.parse.urlsplit(f"//{self.headers.get('Host', '')}").hostname
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https", "capacitor"} or not parsed.hostname:
            return ""
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1", request_host}:
            return ""
        return origin

    def _headers(self, status: HTTPStatus, content_type: str, *, nonce: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        executable = f"'nonce-{nonce}'" if nonce else "'none'"
        self.send_header("Content-Security-Policy", f"default-src 'none'; script-src {executable}; style-src {executable}; connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:
        if self.path not in {"/api/state", "/api/switch"} or not self._allowed_origin():
            self._json(HTTPStatus.FORBIDDEN, {"error": "origin is not allowed"})
            return
        self._headers(HTTPStatus.NO_CONTENT, "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        client = self.client_address[0]
        if self.server.auth_limited(client):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many authentication failures"})
            return False
        provided = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if hmac.compare_digest(provided, expected):
            return True
        self.server.record_auth_failure(client)
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
        return False

    def do_GET(self) -> None:
        if self.path == "/":
            nonce = secrets.token_urlsafe(18)
            body = HTML.replace("__NONCE__", nonce).encode("utf-8")
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", nonce=nonce)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/manifest.webmanifest":
            body = MANIFEST.encode("utf-8")
            self._headers(HTTPStatus.OK, "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/sw.js":
            body = SERVICE_WORKER.encode("utf-8")
            self._headers(HTTPStatus.OK, "text/javascript; charset=utf-8")
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in {"/icon-192.png", "/icon-512.png"}:
            size = 192 if self.path == "/icon-192.png" else 512
            body = _png_icon(size)
            self._headers(HTTPStatus.OK, "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/api/state":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        try:
            self._json(HTTPStatus.OK, state(_running_checkout()))
        except (OSError, RuntimeError, ValueError) as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def do_POST(self) -> None:
        if self.path != "/api/switch":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 1 or length > _MAX_BODY:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid request size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict) or set(payload) != {"target"} or not isinstance(payload["target"], str):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "body must contain only a string target"})
            return
        try:
            result = submit(_running_checkout(), payload["target"])
        except (OSError, RuntimeError, ValueError) as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        self._json(HTTPStatus.ACCEPTED, result)


def create_server(
    *, host: str | None = None, port: int | None = None, token: str | None = None
) -> _Server:
    config = _access_config()
    return _Server((host or config["host"], config["port"] if port is None else port), token or config["token"])

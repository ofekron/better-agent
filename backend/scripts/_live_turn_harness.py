"""Shared plumbing for live e2e scripts that drive a real backend + real turn.

Owns the transport-level harness every live integration script needs: an
isolated uvicorn, headless-auth cookie minting, the WS turn driver, and
run-dir forensics. Scripts own their scenario logic; this module owns the
mechanics, so there is exactly one implementation of each.

Importing this module has no side effects — callers must have already
isolated `BETTER_AGENT_HOME` (via `_test_home`) before importing any
backend module through the helpers here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uvicorn

# Every way a turn can end. Watching only `turn_complete` turns a failed run
# into a full-length timeout instead of an immediate, explained failure.
TERMINAL_FRAMES = frozenset({"turn_complete", "turn_stopped", "turn_detached"})

# Provider-side conditions that say nothing about this repo — the vendor
# account is out of quota or its endpoint is down, so there is no backend
# claim left to test.
_PROVIDER_UNAVAILABLE = ("quota reached", "RESOURCE_EXHAUSTED", "model unreachable")


def cheap_models(env_prefix: str) -> dict[str, str]:
    """Cheapest model per provider kind for live turns, env-overridable per
    script (`{env_prefix}_CLAUDE_MODEL` and friends)."""
    return {
        "claude": os.environ.get(
            f"{env_prefix}_CLAUDE_MODEL", "claude-haiku-4-5-20251001"
        ),
        "codex": os.environ.get(f"{env_prefix}_CODEX_MODEL", "gpt-5.4-mini"),
        "agy": os.environ.get(f"{env_prefix}_AGY_MODEL", "Gemini 3.5 Flash (Low)"),
    }


def ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


def engage_headless_auth(home: Path) -> None:
    """Throwaway credentials for this run only.

    The keychain entries are home-scoped, so an isolated home has none and
    the backend would refuse to start. Headless auth reads the secrets from
    files instead — written here into the temp home and destroyed with it.
    No login is performed: the test signs its own cookie with this secret.
    """
    import bcrypt

    secret_file = home / "session-secret"
    secret_file.write_text(secrets.token_hex(32), encoding="utf-8")
    hash_file = home / "password-hash"
    hash_file.write_text(
        bcrypt.hashpw(secrets.token_hex(16).encode(), bcrypt.gensalt()).decode(),
        encoding="utf-8",
    )
    os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
    os.environ["BETTER_AGENT_USERNAME"] = "integration-test"
    os.environ["BETTER_AGENT_SESSION_SECRET_FILE"] = str(secret_file)
    os.environ["BETTER_AGENT_PASSWORD_HASH_FILE"] = str(hash_file)


def mint_session_cookie() -> str:
    """Sign a session cookie the way Starlette's SessionMiddleware does.
    Read-only against the keychain — never writes credentials."""
    import itsdangerous

    import auth_secrets

    signer = itsdangerous.TimestampSigner(str(auth_secrets.get_session_secret()))
    payload = base64.b64encode(
        json.dumps({"user": {"username": "integration-test"}}).encode()
    )
    return signer.sign(payload).decode("utf-8")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    def __init__(self, app_path: str, port: int) -> None:
        self.port = port
        self.app_path = app_path
        self.server: "uvicorn.Server | None" = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        import uvicorn

        cfg = uvicorn.Config(
            self.app_path, host="127.0.0.1", port=self.port, log_level="warning",
        )
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("uvicorn failed to start in 180s")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def assistant_text(frame: object) -> str:
    """Every assistant text block anywhere in a frame.

    Assistant output rides inside nested `agent_message` envelopes, and the
    flat `content` field carries user prompts rather than agent output. A
    recursive scan for assistant `message.content` blocks reads the same
    words the UI renders without depending on the envelope depth.
    """
    chunks: list[str] = []
    seen: set[tuple[str, str]] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for entry in node:
                walk(entry)
            return
        if not isinstance(node, dict):
            return
        message = node.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            # The same event is re-wrapped at more than one depth; the
            # envelope's uuid identifies the one underlying block.
            uuid = str(node.get("uuid") or "")
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = str(block.get("text") or "")
                if (uuid, text) in seen:
                    continue
                seen.add((uuid, text))
                chunks.append(text)
        for value in node.values():
            walk(value)

    walk(frame)
    return "".join(chunks).strip()


def run_dir_for(home: Path, session_id: str) -> Path | None:
    """The run directory the turn spawned, found by the session id the
    runner was handed. Newest wins if a session ever runs more than once."""
    runs = home / "runs"
    matches: list[tuple[float, Path]] = []
    for candidate in runs.glob("*/input.json"):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("app_session_id") == session_id:
            matches.append((candidate.stat().st_mtime, candidate.parent))
    if not matches:
        return None
    return max(matches)[1]


def run_failure(run_dir: Path | None) -> str:
    """The runner's own verdict on the turn, empty when it succeeded."""
    if run_dir is None:
        return ""
    complete = run_dir / "complete.json"
    if not complete.is_file():
        return ""
    try:
        verdict = json.loads(complete.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if verdict.get("success"):
        return ""
    return str(verdict.get("error") or "the run failed without an error")


def provider_unavailable(reason: str) -> bool:
    return any(marker.lower() in reason.lower() for marker in _PROVIDER_UNAVAILABLE)


class TurnResult:
    """What one turn put on the wire: the errors, and what the model said.

    The text is read from the streamed frames rather than re-fetched after
    `turn_complete`, because the render-tree projection is finalized by
    post-turn work — a REST read racing that finish sees an empty message.
    """

    __slots__ = ("errors", "text")

    def __init__(self, errors: list[str], text: str) -> None:
        self.errors = errors
        self.text = text


async def drive_turn(
    ws_url: str,
    cookie: str,
    sid: str,
    cwd: str,
    prompt: str,
    home: Path,
    model: str | None = None,
    deadline_s: float = 240,
) -> TurnResult:
    """One real turn, observed on the socket the frontend uses.

    `model=None` sends no model override, so the turn runs on whatever the
    session record has stamped — the assertion surface for profile-driven
    selection.
    """
    import websockets

    errors: list[str] = []
    spoken: list[str] = []
    send_payload: dict[str, object] = {
        "type": "send_message",
        "prompt": prompt,
        "app_session_id": sid,
        "cwd": cwd,
    }
    if model is not None:
        send_payload["model"] = model
    try:
        async with websockets.connect(
            ws_url, additional_headers={"Cookie": f"better_agent_session={cookie}"},
        ) as ws:
            await ws.send(json.dumps({"type": "subscribe", "app_session_id": sid}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps(send_payload))
            deadline = time.monotonic() + deadline_s
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    # A run that died at the provider records its verdict on
                    # disk; a failed turn may emit no terminal frame, so
                    # waiting for one would burn the whole budget.
                    failed = run_failure(run_dir_for(home, sid))
                    if failed:
                        errors.append(failed)
                        break
                    continue
                evt = json.loads(raw)
                said = assistant_text(evt)
                if said and said not in spoken:
                    spoken.append(said)
                if evt.get("type") in TERMINAL_FRAMES:
                    break
                if evt.get("type") == "error":
                    errors.append(str(evt.get("data")))
            else:
                errors.append(
                    f"the turn never reached a terminal frame within {deadline_s:.0f}s"
                )
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        errors.append(f"{type(exc).__name__}: {exc}")
    return TurnResult(errors, "".join(spoken).strip())

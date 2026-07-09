#!/usr/bin/env python3
"""CLI driver for the Better Agent orchestration layer.

Exposes the same `Coordinator` the WebSocket `/ws/chat` handler uses,
but from the terminal. Two execution modes:

  in-process   — no backend running. We spin up uvicorn in-process on a
                 free port so the manager MCP tools' HTTP callbacks to
                 the `/api/internal/*` loopback endpoints still round-trip
                 to the same coordinator instance, then drive
                 `coordinator.handle_prompt` directly.
  client       — a backend is already running on the chosen port. We connect
                 to `/ws/chat` as a websocket client and speak the same
                 protocol the frontend uses.

Usage:
  python cli.py                               # REPL, "cli-default" session, cwd=$PWD
  python cli.py -p "do X"                     # one-shot
  python cli.py -p -                          # one-shot, prompt from stdin
  python cli.py --session SID                 # resume a specific session
  python cli.py --mode native|manager         # override session's stored mode
  python cli.py --cwd /path                   # default: $PWD
  python cli.py --provider Z.AI --model glm-5.1
  python cli.py --json                        # jsonl pass-through, no colors
  python cli.py --no-color
  python cli.py --port 8000                   # where to look for an existing backend
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Keep the CLI runnable from any cwd (`python backend/cli.py`).
sys.path.insert(0, str(Path(__file__).parent))

import config_store
from session_manager import manager as session_manager


# Bearer token for authenticating to an auth-gated backend. Set from --token
# (or BETTER_CLAUDE_CLI_TOKEN) in main(); sent as `Authorization: Bearer` on
# REST calls and `?token=` on the /ws/chat upgrade. Empty for backends that
# don't require auth (e.g. a cookie-authed dev session).
_AUTH_TOKEN: Optional[str] = None


def _auth_headers(extra: Optional[dict] = None) -> dict:
    headers = dict(extra or {})
    if _AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {_AUTH_TOKEN}"
    return headers


def _ws_chat_url(port: int) -> str:
    base = f"ws://127.0.0.1:{port}/ws/chat"
    if _AUTH_TOKEN:
        return f"{base}?token={urllib.parse.quote(_AUTH_TOKEN)}"
    return base


# ── ANSI palette ─────────────────────────────────────────────────

DIM = "\033[2m"
BOLD = "\033[1m"
ITALIC = "\033[3m"
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


def _disable_colors() -> None:
    global DIM, BOLD, ITALIC, RESET, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN
    DIM = BOLD = ITALIC = RESET = ""
    RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = ""


def _fmt_tokens(usage: Optional[dict]) -> str:
    if not usage:
        return "-"
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cache_r = usage.get("cache_read_input_tokens", 0) or 0
    cache_c = usage.get("cache_creation_input_tokens", 0) or 0
    total = inp + out
    if total == 0 and cache_r == 0 and cache_c == 0:
        return "-"
    parts = [f"{inp:,} in / {out:,} out"]
    if cache_r or cache_c:
        parts.append(f"cache: {cache_r:,} read / {cache_c:,} write")
    return " · ".join(parts)


def _truncate(s: str, maxlen: int = 120) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) > maxlen:
        return s[: maxlen - 3] + "..."
    return s


def _fmt_args(args) -> str:
    """Compact one-line view of tool args for the pretty renderer."""
    if not isinstance(args, dict):
        return _truncate(str(args), 100)
    # Prefer the most informative field if present.
    for key in ("file_path", "path", "pattern", "command", "prompt", "description"):
        if key in args and args[key]:
            return f"{key}={_truncate(str(args[key]), 80)}"
    try:
        return _truncate(json.dumps(args, default=str), 100)
    except Exception:
        return _truncate(str(args), 100)


# ── Renderers ────────────────────────────────────────────────────

class Renderer:
    """Base: receives raw WS events, writes to stdout."""

    def handle(self, event: dict) -> None:
        raise NotImplementedError

    def turn_started(self, prompt: str) -> None:
        pass

    def turn_finished(self) -> None:
        pass


class JsonRenderer(Renderer):
    def handle(self, event: dict) -> None:
        print(json.dumps(event), flush=True)


class PrettyRenderer(Renderer):
    """Compact colored view of an orchestration turn."""

    def __init__(self) -> None:
        self._at_line_start = True
        self._in_worker = False

    # --- low-level ---
    def _writeln(self, text: str = "") -> None:
        if not self._at_line_start:
            sys.stdout.write("\n")
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        self._at_line_start = True

    def _writeinline(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self._at_line_start = text.endswith("\n")

    # --- public ---
    def turn_started(self, prompt: str) -> None:
        self._at_line_start = True
        self._in_worker = False

    def turn_finished(self) -> None:
        if not self._at_line_start:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._at_line_start = True

    def handle(self, event: dict) -> None:
        etype = event.get("type", "")
        data = event.get("data", {}) or {}

        if etype == "turn_start":
            sid = (data.get("manager_session_id") or "")[:8]
            tag = f"{DIM}{sid}{RESET} " if sid else ""
            self._writeln(f"{MAGENTA}▶ turn{RESET} {tag}")

        elif etype == "manager_event":
            self._render_inner(data.get("event", {}), indent="")

        elif etype == "turn_complete":
            # Intentionally quiet — turn_complete renders the footer.
            pass

        elif etype == "worker_start":
            desc = data.get("worker_description") or ""
            sid = (data.get("worker_session_id") or "")[:8]
            is_new = data.get("is_new", True)
            tag = f"{GREEN}new{RESET}" if is_new else f"{YELLOW}resumed {sid}{RESET}"
            self._writeln(f"  {CYAN}↳ worker{RESET} \"{_truncate(desc, 60)}\" [{tag}]")
            self._in_worker = True

        elif etype == "worker_event":
            self._render_inner(data.get("event", {}), indent="    ")

        elif etype == "worker_complete":
            self._in_worker = False
            tokens = _fmt_tokens(data.get("token_usage"))
            ok = data.get("success", False)
            marker = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
            err = data.get("error")
            err_s = f" {RED}{_truncate(err, 80)}{RESET}" if err else ""
            self._writeln(f"  {CYAN}↳ worker done{RESET} {marker} {DIM}{tokens}{RESET}{err_s}")

        elif etype == "turn_complete":
            tu = data.get("total_token_usage") or {}
            self._writeln()
            self._writeln(f"{DIM}─ turn done · {_fmt_tokens(tu)}{RESET}")

        elif etype == "turn_stopped":
            self._writeln(f"{YELLOW}⏹ stopped{RESET}")

        elif etype == "error":
            err = data.get("error", "")
            self._writeln(f"{RED}error: {_truncate(err, 400)}{RESET}")

        elif etype == "worker_creation_requested":
            desc = data.get("proposed_description", "")
            self._writeln(f"  {GREEN}✓ auto-approved{RESET} worker \"{_truncate(desc, 60)}\"")

        elif etype == "session_renamed":
            name = data.get("name", "")
            self._writeln(f"{DIM}(session renamed to \"{name}\"){RESET}")

        else:
            # Unknown event type — swallow silently in pretty mode.
            pass

    def _render_inner(self, inner: dict, indent: str) -> None:
        itype = inner.get("type", "")
        idata = inner.get("data", {}) or {}

        # New post-refactor shape: the tailer forwards claude's native
        # jsonl line as-is under `agent_message`. Unpack the assistant
        # content blocks into the same output/thinking/tool_call render
        # calls the legacy branches use below. See
        # `frontend/.../MessageBubble.tsx::flattenClaudeMessages` for
        # the mirror of this logic.
        if itype == "agent_message":
            mtype = idata.get("type")
            if mtype != "assistant":
                return
            message = idata.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if not text:
                        continue
                    if indent:
                        lines = text.split("\n")
                        chunk = ("\n" + indent).join(lines)
                        if self._at_line_start:
                            sys.stdout.write(indent)
                        self._writeinline(chunk)
                    else:
                        self._writeinline(text)
                elif btype == "thinking":
                    thought = block.get("thinking") or ""
                    if not thought:
                        continue
                    short = _truncate(thought, 200)
                    self._writeln(f"{indent}{DIM}{ITALIC}… {short}{RESET}")
                elif btype == "tool_use":
                    tool = block.get("name") or "?"
                    if isinstance(tool, str) and tool.startswith("mcp__") and tool.endswith("__delegate"):
                        tool = "delegate"
                    args = block.get("input") or {}
                    self._writeln(
                        f"{indent}{BLUE}🔧 {tool}{RESET}({DIM}{_fmt_args(args)}{RESET})"
                    )
            return

        if itype == "output":
            text = idata.get("output", "") or ""
            if not text:
                return
            # Stream assistant text inline. Keep indent on each new line so
            # worker output lines up even if the text contains `\n`.
            if indent:
                # Rewrite the chunk so every internal newline gets indented.
                lines = text.split("\n")
                chunk = ("\n" + indent).join(lines)
                if self._at_line_start:
                    sys.stdout.write(indent)
                self._writeinline(chunk)
            else:
                self._writeinline(text)

        elif itype == "thinking":
            thought = idata.get("thought", "") or ""
            if not thought:
                return
            short = _truncate(thought, 200)
            self._writeln(f"{indent}{DIM}{ITALIC}… {short}{RESET}")

        elif itype == "tool_call":
            tool = idata.get("tool", "") or "?"
            args = idata.get("args", {}) or {}
            self._writeln(f"{indent}{BLUE}🔧 {tool}{RESET}({DIM}{_fmt_args(args)}{RESET})")

        elif itype == "session_discovered":
            # Too noisy to print.
            pass

        elif itype == "complete":
            # The outer turn_complete/worker_complete handles it.
            pass

        elif itype == "error":
            err = idata.get("error", "")
            self._writeln(f"{indent}{RED}error: {_truncate(err, 400)}{RESET}")


def resolve_provider(selector: Optional[str]) -> Optional[dict]:
    if not selector:
        return None
    providers = config_store.list_providers().get("providers", [])
    by_id = [p for p in providers if p.get("id") == selector]
    if by_id:
        return by_id[0]
    by_name = [p for p in providers if p.get("name", "").casefold() == selector.casefold()]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise SystemExit(f"error: provider name {selector!r} is ambiguous; use its id")
    raise SystemExit(f"error: provider {selector!r} not found")


# ── Backend auto-detection ───────────────────────────────────────

def _probe_backend(port: int, retries: int = 3, timeout: float = 2.0) -> bool:
    """Return True if a Better Agent backend is reachable on localhost:<port>."""
    for attempt in range(retries):
        try:
            for path in ("/api/config", "/api/sessions"):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=_auth_headers())
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status == 200:
                            return True
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            if attempt < retries - 1:
                import time
                time.sleep(0.5)
    return False


def _fetch_backend_session(port: int, session_id: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sessions/{session_id}", headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def _request_json(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: Optional[dict] = None,
    timeout: float = 5.0,
) -> dict:
    data = None
    headers = _auth_headers()
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            decoded = resp.read().decode("utf-8")
            return json.loads(decoded) if decoded else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("detail") if isinstance(payload, dict) else None
        except Exception:
            detail = None
        raise SystemExit(f"error: backend {method} {path} failed: {detail or exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: backend {method} {path} failed: {exc}") from exc


def _list_backend_sessions(port: int) -> list[dict]:
    data = _request_json(port, "/api/sessions")
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return sessions if isinstance(sessions, list) else []


def _create_backend_session(
    *,
    port: int,
    cwd: str,
    model: str,
    mode: Optional[str],
    provider_id: Optional[str],
    worker_creation_policy: Optional[str],
    bare_config: bool,
) -> dict:
    return _request_json(
        port,
        "/api/sessions",
        method="POST",
        body={
            "name": "cli-default",
            "model": model,
            "cwd": cwd,
            "orchestration_mode": mode or "team",
            "source": "cli",
            "provider_id": provider_id,
            "worker_creation_policy": worker_creation_policy or "ask",
            "bare_config": bare_config,
        },
    )


def resolve_backend_session(
    *,
    port: int,
    session_id: Optional[str],
    cwd: str,
    model: str,
    mode: Optional[str],
    provider_id: Optional[str],
    worker_creation_policy: Optional[str] = None,
    bare_config: bool = False,
) -> dict:
    if session_id:
        session = _fetch_backend_session(port, session_id)
        if not session:
            raise SystemExit(f"error: session {session_id} not found")
        if provider_id and session.get("provider_id") != provider_id:
            raise SystemExit("error: --provider does not match the resumed session")
        return session

    for summary in _list_backend_sessions(port):
        if (
            summary.get("name") == "cli-default"
            and summary.get("cwd") == cwd
            and (not provider_id or summary.get("provider_id") == provider_id)
        ):
            existing_id = summary.get("id")
            if existing_id:
                existing = _fetch_backend_session(port, str(existing_id))
                if existing:
                    return existing

    return _create_backend_session(
        port=port,
        cwd=cwd,
        model=model,
        mode=mode,
        provider_id=provider_id,
        worker_creation_policy=worker_creation_policy,
        bare_config=bare_config,
    )


# ── Backend driver — abstract interface ──────────────────────────

class Backend:
    async def start(self) -> None:
        pass

    async def send_prompt(
        self,
        *,
        prompt: str,
        session: dict,
        model: str,
        cwd: str,
        mode: str,
        renderer: Renderer,
        disallowed_tools: Optional[list[str]] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        cli_prompt: Optional[str] = None,
        known_worker_registry_cwds: Optional[dict[str, str]] = None,
    ) -> str:
        """Run one turn. Returns the terminal event type
        ('turn_complete', 'turn_stopped', 'error')."""
        raise NotImplementedError

    async def cancel(self, app_session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


# ── Client backend (talks to a running backend over ws) ──────────

class ClientBackend(Backend):
    def __init__(self, port: int) -> None:
        self.port = port
        self._ws = None

    async def start(self) -> None:
        import websockets
        self._ws = await websockets.connect(
            _ws_chat_url(self.port),
            max_size=None,
            ping_timeout=None,
        )

    def set_worker_creation_policy(self, session_id: str, policy: str) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/sessions/{session_id}/worker_creation_policy",
            data=json.dumps({"worker_creation_policy": policy}).encode("utf-8"),
            headers=_auth_headers({"Content-Type": "application/json"}),
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass

    async def send_prompt(
        self,
        *,
        prompt: str,
        session: dict,
        model: str,
        cwd: str,
        mode: str,
        renderer: Renderer,
        disallowed_tools: Optional[list[str]] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        cli_prompt: Optional[str] = None,
        known_worker_registry_cwds: Optional[dict[str, str]] = None,
    ) -> str:
        assert self._ws is not None, "start() was not called"
        await self._ws.send(json.dumps({
            "type": "send_message",
            "prompt": prompt,
            "app_session_id": session["id"],
            "cwd": cwd,
            "model": model,
            "orchestration_mode": mode,
            "images": [],
            "cli_prompt": cli_prompt,
            "disallowed_tools": disallowed_tools,
            "disabled_builtin_extensions": disabled_builtin_extensions,
            "known_worker_registry_cwds": known_worker_registry_cwds,
            "backend_url": f"http://127.0.0.1:{self.port}",
        }))

        while True:
            raw = await self._ws.recv()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            renderer.handle(event)
            etype = event.get("type", "")
            if etype in ("turn_complete", "turn_stopped", "error"):
                return etype

    async def cancel(self, app_session_id: str) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({
                    "type": "stop_message",
                    "app_session_id": app_session_id,
                }))
            except Exception:
                pass

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass


# ── Main ─────────────────────────────────────────────────────────

def _read_one_shot_prompt(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    return arg


def _load_known_workers_file(path: Optional[str]) -> Optional[list[dict]]:
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("error: --known-workers-file must contain a JSON list")
    for item in data:
        if not isinstance(item, dict):
            raise SystemExit("error: --known-workers-file entries must be objects")
        registry_cwd = item.get("registry_cwd") or item.get("cwd")
        if registry_cwd is None:
            continue
        if not isinstance(registry_cwd, str) or not registry_cwd.strip():
            raise SystemExit("error: known worker registry_cwd must be a non-empty string")
        if not Path(registry_cwd).expanduser().is_absolute():
            raise SystemExit("error: known worker registry_cwd must be absolute")
    return data


def _known_worker_registry_cwds(
    known_workers: Optional[list[dict]],
) -> Optional[dict[str, str]]:
    if not known_workers:
        return None
    out: dict[str, str] = {}
    for item in known_workers:
        sid = item.get("agent_session_id")
        registry_cwd = item.get("registry_cwd") or item.get("cwd")
        if not sid or not registry_cwd:
            continue
        normalized = str(Path(str(registry_cwd)).expanduser().resolve())
        existing = out.get(str(sid))
        if existing and existing != normalized:
            raise SystemExit(f"error: conflicting registry_cwd for known worker {sid}")
        out[str(sid)] = normalized
    return out or None


def _build_cli_prompt_override(
    *,
    session: dict,
    cwd: str,
    prompt: str,
    mode: str,
    known_workers: Optional[list[dict]],
) -> Optional[str]:
    if mode != "manager" or known_workers is None:
        return None
    # Bare (TestApe-isolated) sessions get an EMPTY system prompt — no BC
    # manager bootstrap. The caller's prompt is the complete contract.
    if session.get("bare_config"):
        return None
    from orchs.manager import bootstrap as manager_bootstrap

    is_first_turn = session.get("agent_session_id") is None
    return manager_bootstrap.build_wrapped_prompt(
        cwd,
        prompt,
        is_first_turn,
        known_workers=known_workers,
        self_session_id=str(session.get("id") or ""),
        self_role="manager",
        self_description=str(session.get("name") or "manager"),
        manager_session_id=str(session.get("id") or ""),
        manager_description=str(session.get("name") or "manager"),
    )


async def _drive_turn(
    *,
    backend: Backend,
    renderer: Renderer,
    prompt: str,
    session: dict,
    model: str,
    cwd: str,
    mode: str,
    disallowed_tools: Optional[list[str]] = None,
    disabled_builtin_extensions: Optional[list[str]] = None,
    cli_prompt: Optional[str] = None,
    known_worker_registry_cwds: Optional[dict[str, str]] = None,
) -> str:
    # Mirror the REST middleware: every inbound user action is recorded
    # as `command_received` in the target session's events.jsonl BEFORE
    # the turn runs. Best-effort — ingest failure must not block the
    # turn (the user_message_* lifecycle still publishes downstream).
    try:
        from event_journal import publish_event
        root_id = session_manager._root_id_for(session["id"]) or session["id"]
        await publish_event(
            session_id=root_id,
            context_id=session["id"],
            event_type="command_received",
            data={
                "method": "CLI",
                "path": "send_prompt",
                "sid": session["id"],
                "payload": {
                    "prompt": prompt, "cwd": cwd,
                    "model": model, "mode": mode,
                },
                "uuid": str(uuid.uuid4()),
            },
            source="cli",
        )
    except Exception:
        logger.exception("CLI command_received ingest failed")
    renderer.turn_started(prompt)
    try:
        return await backend.send_prompt(
            prompt=prompt,
            session=session,
            model=model,
            cwd=cwd,
            mode=mode,
            renderer=renderer,
            disallowed_tools=disallowed_tools,
            disabled_builtin_extensions=disabled_builtin_extensions,
            cli_prompt=cli_prompt,
            known_worker_registry_cwds=known_worker_registry_cwds,
        )
    finally:
        renderer.turn_finished()


async def _repl(
    *,
    backend: Backend,
    renderer: Renderer,
    session: dict,
    model: str,
    cwd: str,
    mode: str,
    known_workers: Optional[list[dict]] = None,
) -> int:
    known_worker_registry_cwds = _known_worker_registry_cwds(known_workers)
    # SIGINT during a turn → cancel; SIGINT between turns → normal interrupt.
    current_turn: dict[str, Optional[asyncio.Task]] = {"task": None}

    def handle_sigint() -> None:
        task = current_turn["task"]
        if task and not task.done():
            asyncio.create_task(backend.cancel(session["id"]))
        else:
            raise KeyboardInterrupt()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, handle_sigint)
    except NotImplementedError:
        pass  # Windows

    last_exit = 0
    while True:
        try:
            line = await asyncio.to_thread(input, f"{BOLD}>{RESET} ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("exit", "quit"):
            break

        task = asyncio.create_task(_drive_turn(
            backend=backend,
            renderer=renderer,
            prompt=line,
            session=session,
            model=model,
            cwd=cwd,
            mode=mode,
            cli_prompt=_build_cli_prompt_override(
                session=session,
                cwd=cwd,
                prompt=line,
                mode=mode,
                known_workers=known_workers,
            ),
            known_worker_registry_cwds=known_worker_registry_cwds,
        ))
        current_turn["task"] = task
        try:
            terminal = await task
        except Exception as e:
            renderer.handle({"type": "error", "data": {"error": f"{type(e).__name__}: {e}"}})
            terminal = "error"
        finally:
            current_turn["task"] = None

        if terminal == "error":
            last_exit = 1

    return last_exit


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CLI driver for the Better Agent orchestration layer.",
    )
    p.add_argument("-p", "--prompt", help="one-shot prompt (use '-' to read from stdin)")
    p.add_argument("--session", help="resume a specific session id")
    p.add_argument("--mode", choices=["team", "native"], help="orchestration mode")
    p.add_argument("--cwd", default=os.getcwd(), help="working directory (default: $PWD)")
    p.add_argument("--provider", help="provider id or unique provider name")
    p.add_argument("--model", help="model id (default: provider/session default)")
    p.add_argument("--json", action="store_true", help="emit raw jsonl events instead of pretty output")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--port", type=int, default=8000, help="port of the running backend to connect to (default 8000)")
    p.add_argument(
        "--token",
        help="bearer token to authenticate to an auth-gated backend (default: $BETTER_CLAUDE_CLI_TOKEN)",
    )
    p.add_argument(
        "--bare-config",
        action="store_true",
        dest="bare_config",
        help=(
            "Run this session with an EMPTY system prompt: no skills, no "
            "CLAUDE.md/memory, no injected instructions/bootstrap — only the "
            "prompt passed in. MCP tools and the manager `delegate` tool are "
            "retained. Used by TestApe-isolated manager/worker sessions."
        ),
    )
    p.add_argument(
        "--worker-creation-policy",
        choices=["ask", "approve", "deny"],
        help="Fresh worker policy for manager sessions: ask, approve, or deny.",
    )
    p.add_argument(
        "--disallowed-tool",
        action="append",
        dest="disallowed_tools",
        default=[],
        help="Disallow a provider tool for this CLI turn. May be repeated.",
    )
    p.add_argument(
        "--disabled-builtin-extension",
        action="append",
        dest="disabled_builtin_extensions",
        default=[],
        help="Disable a built-in/runtime extension for this CLI turn. May be repeated.",
    )
    p.add_argument(
        "--known-workers-file",
        help=(
            "JSON file containing the exact worker list to render in "
            "<known_workers> for manager mode."
        ),
    )
    return p.parse_args()


async def _async_main(args: argparse.Namespace) -> int:
    global _AUTH_TOKEN
    _AUTH_TOKEN = getattr(args, "token", None) or os.environ.get("BETTER_CLAUDE_CLI_TOKEN")
    if args.json or args.no_color or not sys.stdout.isatty():
        _disable_colors()
    renderer: Renderer = JsonRenderer() if args.json else PrettyRenderer()

    cwd = os.path.abspath(args.cwd)
    if not _probe_backend(args.port):
        print(
            f"{RED}error: no Better Agent backend reachable on 127.0.0.1:{args.port}{RESET}",
            file=sys.stderr,
        )
        print("Start Better Agent first or pass --port for the running backend.", file=sys.stderr)
        return 2

    selected_provider = resolve_provider(args.provider)
    provider_id = selected_provider.get("id") if selected_provider else None
    requested_model = (
        args.model
        or (selected_provider or {}).get("default_model")
        or config_store.default_session_model()
    )
    session = resolve_backend_session(
        port=args.port,
        session_id=args.session,
        cwd=cwd,
        model=requested_model,
        mode=args.mode,
        provider_id=provider_id,
        worker_creation_policy=args.worker_creation_policy,
        bare_config=bool(getattr(args, "bare_config", False)),
    )
    mode = args.mode or session.get("orchestration_mode") or "team"
    model = args.model or session.get("model") or requested_model
    known_workers = _load_known_workers_file(args.known_workers_file)
    known_worker_registry_cwds = _known_worker_registry_cwds(known_workers)
    config_store.apply_env_vars(session.get("provider_id"))

    backend: Backend = ClientBackend(args.port)
    banner = f"{DIM}connected to running backend on :{args.port}{RESET}"

    if not args.json:
        sys.stdout.write(
            f"{DIM}session {session['id'][:8]} · "
            f"{mode} mode · cwd={_truncate(cwd, 60)}{RESET}\n"
            f"{banner}\n"
        )
        sys.stdout.flush()

    try:
        await backend.start()
        if args.worker_creation_policy:
            backend.set_worker_creation_policy(
                session["id"], args.worker_creation_policy,
            )
    except Exception as e:
        print(f"{RED}failed to start backend: {e}{RESET}", file=sys.stderr)
        return 2

    try:
        if args.prompt is not None:
            prompt = _read_one_shot_prompt(args.prompt)
            terminal = await _drive_turn(
                backend=backend,
                renderer=renderer,
                prompt=prompt,
                session=session,
                model=model,
                cwd=cwd,
                mode=mode,
                disallowed_tools=args.disallowed_tools or None,
                disabled_builtin_extensions=args.disabled_builtin_extensions or None,
                cli_prompt=_build_cli_prompt_override(
                    session=session,
                    cwd=cwd,
                    prompt=prompt,
                    mode=mode,
                    known_workers=known_workers,
                ),
                known_worker_registry_cwds=known_worker_registry_cwds,
            )
            return 0 if terminal == "turn_complete" else 1
        else:
            return await _repl(
                backend=backend,
                renderer=renderer,
                session=session,
                model=model,
                cwd=cwd,
                mode=mode,
                known_workers=known_workers,
            )
    finally:
        await backend.close()


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

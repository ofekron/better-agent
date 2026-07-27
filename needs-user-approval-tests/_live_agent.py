"""Shared harness for the live cross-vendor built-in-MCP suite.

Every case in this directory drives a REAL vendor CLI subprocess through the
real provider abstraction and then asserts on backend-owned state. Nothing
here mocks a model, a runner, or an MCP transport.

Run the suite through `run_live_mcp_tests.py`, not directly — it owns the
opt-in gate, home isolation, and backend lifecycle.

Single source of truth for three things the cases must agree on:

* `VENDORS` — one row per runnable provider kind plus the cheapest capable
  model that vendor sells. Adding a vendor is one row.
* `builtin_servers_for()` — which built-in MCP servers a run of that kind is
  configured with. Computed by calling the production assembler rather than
  restating it, so the table cannot drift from `builtin_mcp_config`.
* `RUNNERS_*` — which runner modules actually register those servers with
  their CLI. `builtin_mcp_config` computes a server set for every kind, but a
  runner that never calls the assembler ships none of them; the intersection
  is what a live agent can really call.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"

# Runners that call `builtin_mcp_config.with_builtin_mcp_servers`, i.e. the
# only runners whose CLI is told about `ui` / `open-config-panel` /
# `capabilities`. Every other runner hosts the operation broker but registers
# no built-in MCP server at all.
RUNNERS_WITH_BUILTIN_MCP = frozenset({
    "runner",           # Claude — in-process SDK servers, not stdio
    "runner_codex",
    "runner_agy",
})

# Runners that expose the `communicate` tool set as a real MCP server.
RUNNERS_WITH_COMMUNICATE_MCP = frozenset({"runner"})

# Runners that expose the same tools as per-turn dynamic tools instead of an
# MCP server. Functionally equivalent from the model's side; the assertion is
# on the tool's backend side effect either way.
RUNNERS_WITH_DYNAMIC_COMMUNICATE = frozenset({"runner_codex"})


@dataclass(frozen=True)
class Vendor:
    """One vendor row: how to reach it and the cheapest model it sells.

    `model` must be a literal member of that provider's catalog — several
    providers validate `model` by exact membership and raise otherwise.

    `mode` is the provider auth mode. It is not cosmetic: `config_store`
    rejects an openai provider created in subscription mode outright, so that
    kind only exists here as an api-key provider and is skipped when the key
    is absent.
    """

    kind: str
    cli: str | None
    model: str
    mode: str = "subscription"
    api_key_env: str | None = None

    @property
    def runner_module(self) -> str | None:
        import provider_manifest

        spec = provider_manifest.spec_for(self.kind)
        return spec.runner_module if spec else None

    def cli_path(self) -> str | None:
        if self.cli is None:
            return None
        import cli_paths

        return cli_paths.resolve_cli_binary(self.cli)

    def api_key(self) -> str:
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "").strip()


VENDORS: tuple[Vendor, ...] = (
    Vendor("claude", "claude", "claude-haiku-4-5-20251001"),
    Vendor("codex", "codex", "gpt-5.4-mini"),
    # The gemini CLI is gone; Antigravity (`agy`) is the replacement path for
    # the same models and carries that coverage.
    Vendor("agy", "agy", "gemini-3.5-flash-medium"),
    Vendor("fugu", "codex", "fugu"),
    Vendor("copilot", "copilot", "gpt-5-mini"),
    Vendor("cursor", "cursor-agent", "composer-1"),
    Vendor("qwen", "qwen", "qwen3-coder-flash"),
    Vendor("kimi", "kimi", "kimi-k2-turbo-preview"),
    Vendor("amp", "amp", "free"),
    Vendor("opencode", "opencode", "opencode/deepseek-v4-flash-free"),
    Vendor("pi", "pi", "anthropic/claude-haiku-4-5"),
)

VENDORS_BY_KIND: dict[str, Vendor] = {v.kind: v for v in VENDORS}


@contextlib.contextmanager
def _probe_runtime_broker():
    """Present a runtime broker address for the duration of a wiring probe.

    `with_builtin_mcp_servers` refuses to inject anything without one, and
    every runner establishes it at spawn via
    `runner_operation_host.hydrate_runner_inputs`. Probing without it reports
    an empty server set for every provider — a property of the probe, not of
    the wiring.
    """
    names = ("BETTER_CLAUDE_RUNTIME_BROKER", "BETTER_AGENT_RUNTIME_BROKER")
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ[name] = "127.0.0.1:1"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _probe_inputs(kind: str, user_facing: bool = True) -> dict[str, Any]:
    return {
        "app_session_id": "wiring-probe",
        "backend_url": "http://127.0.0.1:1",
        "cwd": str(REPO_ROOT),
        "model": "",
        "provider_id": "wiring-probe",
        "provider_kind": kind,
        "user_facing": user_facing,
    }


def builtin_servers_for(kind: str, *, user_facing: bool = True) -> frozenset[str]:
    """Built-in MCP servers a live run of `kind` can actually call.

    The production assembler decides the set; the runner table decides whether
    the assembler is ever consulted for that kind.
    """
    import builtin_mcp_config
    import provider_manifest

    spec = provider_manifest.spec_for(kind)
    if spec is None or spec.runner_module not in RUNNERS_WITH_BUILTIN_MCP:
        return frozenset()

    with _probe_runtime_broker():
        assembled = builtin_mcp_config.with_builtin_mcp_servers(
            _probe_inputs(kind, user_facing), {}
        )
    servers = set(assembled.get("mcp_servers") or {})
    if spec.runner_module in RUNNERS_WITH_COMMUNICATE_MCP:
        servers.add("communicate")
    return frozenset(servers)


def supports_communicate(kind: str) -> bool:
    """True when the kind exposes the communicate tool set at all — as an MCP
    server or as per-turn dynamic tools."""
    import provider_manifest

    spec = provider_manifest.spec_for(kind)
    if spec is None:
        return False
    return spec.runner_module in (
        RUNNERS_WITH_COMMUNICATE_MCP | RUNNERS_WITH_DYNAMIC_COMMUNICATE
    )


def vendors_for_server(server: str) -> tuple[Vendor, ...]:
    if server == "communicate":
        return tuple(v for v in VENDORS if supports_communicate(v.kind))
    return tuple(v for v in VENDORS if server in builtin_servers_for(v.kind))


def vendors_without_server(server: str) -> tuple[Vendor, ...]:
    covered = {v.kind for v in vendors_for_server(server)}
    return tuple(v for v in VENDORS if v.kind not in covered)


class Skip(Exception):
    """Raised by a case when its precondition is absent (CLI not installed,
    catalog empty). Reported separately from a failure — a missing vendor is
    not a broken vendor."""


@dataclass(frozen=True)
class TurnResult:
    """One completed turn: the runner's `complete` payload and every event the
    provider emitted, which is what read-only tools are asserted against."""

    complete: dict
    events: list


# Runner errors that mean the vendor account cannot serve the turn — an
# exhausted or unauthorized account is an environment condition, not a broken
# MCP server, so these skip instead of failing the suite.
_ENVIRONMENT_ERROR_MARKERS = (
    "quota reached",
    "quota exceeded",
    "rate limit",
    "resets in",
    "upgrade your subscription",
    "not authenticated",
    "unauthorized",
    "invalid api key",
    "credit balance",
)


def _raise_for_runner_error(vendor: "Vendor", error: str, run_dir) -> None:
    lowered = error.lower()
    if any(marker in lowered for marker in _ENVIRONMENT_ERROR_MARKERS):
        raise Skip(f"{vendor.kind} account cannot serve this turn: {error}")
    raise AssertionError(
        f"{vendor.kind}: runner error {error!r} run_dir={run_dir}"
    )


@dataclass(frozen=True)
class Case:
    """One assertion against one vendor.

    `run` receives the live `LiveBackend` and a scratch cwd, and either
    returns (pass), raises `Skip`, or raises anything else (fail).
    """

    server: str
    tool: str
    vendor: Vendor
    run: Any

    @property
    def name(self) -> str:
        return f"{self.server}.{self.tool}[{self.vendor.kind}]"


def require_cli(vendor: Vendor) -> str:
    path = vendor.cli_path()
    if not path:
        raise Skip(f"{vendor.cli} CLI is not installed")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001 — probe until it answers
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"backend did not become ready: {last_error}")


class LiveBackend:
    """A real backend serving loopback MCP traffic for the whole suite.

    One instance per pytest session: booting uvicorn and activating an
    installation profile is the expensive part, and every vendor run is
    isolated by its own provider record and session id anyway.
    """

    def __init__(self) -> None:
        self.port = _free_port()
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._provider_ids: dict[str, str] = {}
        self._instances: list[Any] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        import logging

        import uvicorn

        # The backend's INFO-level perf rollups drown the case results, which
        # are the only output this suite exists to produce.
        logging.getLogger().setLevel(logging.WARNING)
        for name in ("perf", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.WARNING)

        import main

        self.main = main
        self._server = uvicorn.Server(
            uvicorn.Config(
                main.app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="on",
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        _wait_for_server(f"{self.url}/api/auth/needs_setup")

    def stop(self) -> None:
        # Cancel before the server goes down: a CLI still mid-turn would
        # otherwise survive the suite and keep spending.
        for instance in self._instances:
            try:
                instance.cancel_all()
            except Exception:  # noqa: BLE001 — teardown must not mask results
                pass
        self._instances.clear()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15.0)

    def provider_id(self, vendor: Vendor) -> str:
        """A persisted provider record per kind — model validation and
        credential routing both read it, so a bare dict will not do."""
        cached = self._provider_ids.get(vendor.kind)
        if cached:
            return cached
        import config_store

        payload = {
            "name": f"live-{vendor.kind}",
            "kind": vendor.kind,
            "mode": vendor.mode,
            "default_model": vendor.model,
        }
        if vendor.mode == "api_key":
            key = vendor.api_key()
            if not key:
                raise Skip(
                    f"{vendor.kind} needs an api key; set {vendor.api_key_env}"
                )
            payload["api_key"] = key
        record = config_store.add_provider(payload)
        self._provider_ids[vendor.kind] = record["id"]
        return record["id"]

    def new_session(self, vendor: Vendor, name: str, cwd: str) -> str:
        import session_store

        session = session_store.create_session(
            name=name,
            model=vendor.model,
            cwd=cwd,
            orchestration_mode="native",
            source="cli",
            provider_id=self.provider_id(vendor),
            browser_harness_enabled=False,
        )
        return str(session["id"])

    async def run_turn(
        self,
        vendor: Vendor,
        *,
        sid: str,
        prompt: str,
        cwd: str,
        timeout_s: float = 300.0,
    ) -> dict:
        """Drive one real turn and return the runner's `complete` payload.

        Raises on runner error or timeout — a vendor that cannot complete a
        trivial tool-call turn is a failure, not a skip.
        """
        import provider as provider_module
        import config_store

        provider_cls = provider_module._resolve_class(vendor.kind)
        record = config_store.get_provider(self.provider_id(vendor))
        instance = provider_cls(record)
        self._instances.append(instance)
        run_id = f"live-{vendor.kind}-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue = asyncio.Queue()
        instance.start_run(
            run_id=run_id,
            prompt=prompt,
            cwd=cwd,
            loop=asyncio.get_running_loop(),
            queue=queue,
            model=vendor.model,
            reasoning_effort=None,
            session_id=None,
            mode="native",
            app_session_id=sid,
            backend_url=self.url,
            internal_token=self.main.coordinator.internal_token,
            browser_harness_enabled=False,
            user_facing=True,
            provider_run_config={},
            capability_contexts=[],
            setting_sources=[],
        )
        run_dir = instance._runs[run_id].run_dir

        seen: list[str] = []
        events: list[Any] = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                event = await asyncio.wait_for(queue.get(), timeout=min(5.0, remaining))
            except asyncio.TimeoutError:
                continue
            seen.append(event.type)
            events.append(event)
            if event.type == "complete":
                complete = dict(event.data or {})
                error = str(complete.get("error") or "")
                if error:
                    _raise_for_runner_error(vendor, error, run_dir)
                return TurnResult(complete=complete, events=events)

        # An agent that blew the deadline is still running, and some CLIs are
        # spawned with a multi-hour print timeout. Leaving it alive would burn
        # quota for the rest of the suite and outlive the process entirely.
        instance.cancel_run(run_id)
        raise AssertionError(
            f"{vendor.kind}: no complete within {timeout_s}s; events={seen} "
            f"run_dir={run_dir}"
        )


def tool_calls(events: Iterable[Any], names: Iterable[str]) -> list[dict]:
    """tool_use blocks in a turn's event stream whose name matches `names`.

    Reads the provider's own event stream rather than the persisted render
    tree: this harness drives `Provider.start_run` directly, so nothing goes
    through the orchestrator that would populate `session.messages`. The stream
    is the same data the orchestrator would project, taken one step earlier.

    Matching is suffix-based because providers namespace MCP tools differently
    (`mcp__ui__open_file_panel`, `ui__open_file_panel`, ...).
    """
    wanted = tuple(names)
    found: list[dict] = []
    for event in events:
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            continue
        for block in _content_blocks(data):
            name = str(block.get("name") or "")
            if not name:
                continue
            if name in wanted or name.rsplit("__", 1)[-1] in wanted:
                found.append(block)
    return found


def tool_prompt(server: str, tool: str, instruction: str) -> str:
    """The standard instruction for making an agent call one built-in tool.

    Providers may present built-in MCP tools as DEFERRED tools: the schema is
    not in the initial tool list and the agent has to look it up (`ToolSearch`
    or the provider's equivalent) before it can call anything. Without this
    hint an agent burns the turn searching and never places the call, which
    reads as "the server is broken" when it is not.
    """
    return (
        "This is an automated integration test of Better Agent tool injection. "
        f"Call the MCP tool named {tool} from the {server!r} server exactly "
        f"once. {instruction} "
        f"If that tool is not in your available tools yet, first look it up "
        f"(for example with ToolSearch for '{tool}' or '{server}'), then call "
        "it. Do not call any other tool for any other purpose. When the tool "
        "has returned, reply with the single word: done"
    )


def observed_tools(events: Iterable[Any]) -> list[str]:
    """Every tool name the turn emitted, in order.

    Attached to tool-call assertion failures so the report distinguishes "the
    server was missing" from "the model called something else" without a
    re-run.
    """
    names: list[str] = []
    for event in events:
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            continue
        for block in _content_blocks(data):
            name = str(block.get("name") or "")
            if name:
                names.append(name)
    return names


def _content_blocks(data: dict) -> list[dict]:
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else data.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    return []

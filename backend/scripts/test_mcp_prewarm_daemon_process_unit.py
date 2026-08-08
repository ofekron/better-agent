"""Dedicated unit owner for `mcp_prewarm/daemon_process.py`.

`daemon_process.py` is the long-lived warm-pool daemon launched as a
subprocess by `supervisor._spawn` (`python3 daemon_process.py <cfg>`);
it is never imported by any pytest test, so it had no unit coverage.
These tests drive every function/branch directly: async functions run
under asyncio via anyio's asyncio backend, with collaborators
(`_require_posix_peer`, `_write_state`, the anyio listener factories,
`os._exit`, `_build_mcp_server`) patched at the module boundary. No
source edit, no pragma, no real sockets.
"""

from __future__ import annotations

import asyncio
import json
import time

import anyio
import pytest

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from mcp_prewarm import daemon_process as dp


REQ = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
REQ_LINE = (json.dumps(REQ) + "\n").encode()
RESP = JSONRPCMessage.model_validate(
    {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
)


def run(coro):
    return asyncio.run(coro)


class FakeServer:
    """Low-level MCP server shape: run() + create_initialization_options()."""

    def create_initialization_options(self):
        return "INITOPTS"

    async def run(self, rs, ws, init_options):  # pragma: no cover - overridden per test
        raise AssertionError("FakeServer.run should be subclassed")


class FakeStream:
    """anyio SocketStream shape used by the pump loop."""

    def __init__(self, chunks=None, send_exc=None, aclose_exc=None):
        self._chunks = list(chunks or [])
        self._send_exc = send_exc
        self._aclose_exc = aclose_exc
        self.sent = []
        self.closed = False

    async def receive(self, n=65536):
        if not self._chunks:
            raise anyio.EndOfStream()
        chunk = self._chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    async def send(self, data):
        if self._send_exc is not None:
            raise self._send_exc
        self.sent.append(data)

    async def aclose(self):
        self.closed = True
        if self._send_exc is not None or self._aclose_exc is not None:
            if self._aclose_exc is not None:
                raise self._aclose_exc

    def extra(self, attr):
        return object()


# --------------------------------------------------------------------------- #
# _Activity
# --------------------------------------------------------------------------- #

def test_activity_init_and_touch():
    act = dp._Activity()
    assert act.connections == 0
    assert act.last_active > 0
    before = act.last_active
    act.touch()
    assert act.last_active >= before


# --------------------------------------------------------------------------- #
# _load_spawn_config
# --------------------------------------------------------------------------- #

def test_load_spawn_config(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"x": 1}), encoding="utf-8")
    assert dp._load_spawn_config(cfg) == {"x": 1}


# --------------------------------------------------------------------------- #
# _sessions_dir
# --------------------------------------------------------------------------- #

def test_sessions_dir_ok(tmp_path):
    assert dp._sessions_dir({"sessions_dir": str(tmp_path)}) == tmp_path


@pytest.mark.parametrize("value", [{}, {"sessions_dir": ""}, {"sessions_dir": 123}])
def test_sessions_dir_invalid(value):
    with pytest.raises(RuntimeError, match="non-empty string"):
        dp._sessions_dir(value)


def test_sessions_dir_relative_rejected():
    with pytest.raises(RuntimeError, match="must be absolute"):
        dp._sessions_dir({"sessions_dir": "relative/path"})


# --------------------------------------------------------------------------- #
# _load_entrypoint
# --------------------------------------------------------------------------- #

def test_load_entrypoint_too_short():
    with pytest.raises(RuntimeError, match="entrypoint"):
        dp._load_entrypoint([])
    with pytest.raises(RuntimeError, match="entrypoint"):
        dp._load_entrypoint(["-m"])


def test_load_entrypoint_not_module_flag():
    with pytest.raises(RuntimeError, match="entrypoint"):
        dp._load_entrypoint(["run", "x"])


def test_load_entrypoint_module():
    import json as json_mod

    mod, extra = dp._load_entrypoint(["-m", "json"])
    assert mod is json_mod
    assert extra == []


def test_load_entrypoint_script_incomplete():
    with pytest.raises(RuntimeError, match="incomplete"):
        dp._load_entrypoint(["-m", "better_agent_sdk.script_entrypoint"])


def test_load_entrypoint_script_full(monkeypatch):
    import better_agent_sdk.script_entrypoint as se

    captured = {}

    def fake_load(path, name, rest):
        captured.update(path=path, name=name, rest=rest)
        return "BUILT"

    monkeypatch.setattr(se, "load_script_module", fake_load)
    mod, extra = dp._load_entrypoint(
        ["-m", "better_agent_sdk.script_entrypoint", "P", "N", "r1", "r2"]
    )
    assert mod == "BUILT"
    assert extra == ["r1", "r2"]
    assert captured == {"path": "P", "name": "N", "rest": ["r1", "r2"]}


# --------------------------------------------------------------------------- #
# _build_mcp_server
# --------------------------------------------------------------------------- #

def test_build_mcp_server_no_build_server(monkeypatch):
    monkeypatch.setattr(dp.importlib, "import_module", lambda name: type("M", (), {})())
    with pytest.raises(RuntimeError, match="build_server"):
        dp._build_mcp_server(["-m", "x"])


def test_build_mcp_server_build_not_callable(monkeypatch):
    class M:
        build_server = "not callable"

    monkeypatch.setattr(dp.importlib, "import_module", lambda name: M)
    with pytest.raises(RuntimeError, match="build_server"):
        dp._build_mcp_server(["-m", "x"])


def test_build_mcp_server_no_run_method(monkeypatch):
    class M:
        @staticmethod
        def build_server():
            return type("NoRun", (), {})()

    monkeypatch.setattr(dp.importlib, "import_module", lambda name: M)
    with pytest.raises(RuntimeError, match="FastMCP or mcp.server.Server"):
        dp._build_mcp_server(["-m", "x"])


def test_build_mcp_server_run_without_init_options(monkeypatch):
    class S:
        async def run(self, *a):
            pass

    class M:
        @staticmethod
        def build_server():
            return S()

    monkeypatch.setattr(dp.importlib, "import_module", lambda name: M)
    with pytest.raises(RuntimeError, match="FastMCP or mcp.server.Server"):
        dp._build_mcp_server(["-m", "x"])


def test_build_mcp_server_lowlevel_server(monkeypatch):
    srv = FakeServer()

    class M:
        @staticmethod
        def build_server():
            return srv

    monkeypatch.setattr(dp.importlib, "import_module", lambda name: M)
    assert dp._build_mcp_server(["-m", "x"]) is srv


def test_build_mcp_server_fastmcp_wrapper(monkeypatch):
    srv = FakeServer()

    class FastMCP:
        def __init__(self, inner):
            self._mcp_server = inner

    class M:
        @staticmethod
        def build_server():
            return FastMCP(srv)

    monkeypatch.setattr(dp.importlib, "import_module", lambda name: M)
    assert dp._build_mcp_server(["-m", "x"]) is srv


def test_build_mcp_server_sets_argv_from_module_file(monkeypatch):
    captured = {}
    srv = FakeServer()

    class M:
        __file__ = "/path/to/mod.py"

        @staticmethod
        def build_server():
            captured["argv"] = list(__import__("sys").argv)
            return srv

    monkeypatch.setattr(dp.importlib, "import_module", lambda name: M)
    dp._build_mcp_server(["-m", "x", "extra1", "extra2"])
    assert captured["argv"] == ["/path/to/mod.py", "extra1", "extra2"]


def test_build_mcp_server_argv_default_when_no_file(monkeypatch):
    captured = {}
    srv = FakeServer()

    class M:
        @staticmethod
        def build_server():
            captured["argv"] = list(__import__("sys").argv)
            return srv

    monkeypatch.setattr(dp.importlib, "import_module", lambda name: M)
    dp._build_mcp_server(["-m", "x"])
    assert captured["argv"] == ["mcp-server"]


# --------------------------------------------------------------------------- #
# _write_state
# --------------------------------------------------------------------------- #

def test_write_state(tmp_path):
    state = tmp_path / "state.json"
    dp._write_state(state, {"a": 1})
    assert json.loads(state.read_text()) == {"a": 1}
    assert not (tmp_path / "state.json.tmp").exists()


# --------------------------------------------------------------------------- #
# _session_exists
# --------------------------------------------------------------------------- #

def test_session_exists_json(tmp_path):
    (tmp_path / "sid.json").write_text("{}")
    assert dp._session_exists("sid", tmp_path) is True


def test_session_exists_dir(tmp_path):
    (tmp_path / "sid").mkdir()
    assert dp._session_exists("sid", tmp_path) is True


def test_session_exists_missing(tmp_path):
    assert dp._session_exists("nope", tmp_path) is False


# --------------------------------------------------------------------------- #
# _pump_connection
# --------------------------------------------------------------------------- #

def test_pump_connection_permission_denied(monkeypatch):
    def deny(raw):
        raise PermissionError("peer mismatch")

    monkeypatch.setattr(dp, "_require_posix_peer", deny)
    stream = FakeStream()
    run(dp._pump_connection(stream, object(), "io", dp._Activity()))
    assert stream.closed is True


def test_pump_connection_dispatches_to_core(monkeypatch):
    monkeypatch.setattr(dp, "_require_posix_peer", lambda raw: None)
    captured = {}

    async def fake_core(stream, srv, io, act):
        captured["called"] = (stream, srv, io, act)

    monkeypatch.setattr(dp, "_pump_connection_core", fake_core)
    stream = FakeStream()
    act = dp._Activity()
    run(dp._pump_connection(stream, "SRV", "IO", act))
    assert captured["called"][0] is stream
    assert captured["called"][1] == "SRV"
    assert captured["called"][3] is act


# --------------------------------------------------------------------------- #
# _pump_tcp_connection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "exc", [ConnectionError("x"), anyio.EndOfStream(), ValueError("x")]
)
def test_pump_tcp_secret_read_failure(monkeypatch, exc):
    async def boom(receive):
        raise exc

    monkeypatch.setattr(dp.tcp_transport, "read_secret_frame", boom)
    stream = FakeStream()
    run(dp._pump_tcp_connection(stream, "SRV", "IO", dp._Activity(), "secret"))
    assert stream.closed is True


def test_pump_tcp_secret_mismatch(monkeypatch):
    async def ok(receive):
        return b"wrong"

    monkeypatch.setattr(dp.tcp_transport, "read_secret_frame", ok)
    stream = FakeStream()
    run(dp._pump_tcp_connection(stream, "SRV", "IO", dp._Activity(), "secret"))
    assert stream.closed is True


def test_pump_tcp_secret_match_calls_core(monkeypatch):
    async def ok(receive):
        return b"secret"

    monkeypatch.setattr(dp.tcp_transport, "read_secret_frame", ok)
    captured = {}

    async def fake_core(stream, srv, io, act):
        captured["ok"] = True

    monkeypatch.setattr(dp, "_pump_connection_core", fake_core)
    stream = FakeStream()
    run(dp._pump_tcp_connection(stream, "SRV", "IO", dp._Activity(), "secret"))
    assert captured.get("ok") is True


# --------------------------------------------------------------------------- #
# _pump_connection_core
# --------------------------------------------------------------------------- #

class _Server:
    """Configurable MCP server double used by the core pump tests."""

    def __init__(self, resp=None):
        self.resp = resp

    async def run(self, rs, ws, init_options):
        async with rs, ws:
            if self.resp is None:
                # Consume any items (including parse exceptions) until closed.
                async for _ in rs:
                    pass
                return
            async for item in rs:
                if isinstance(item, Exception):
                    continue
                await ws.send(SessionMessage(self.resp))
                break


def test_core_roundtrip():
    stream = FakeStream(chunks=[REQ_LINE])
    act = dp._Activity()
    run(dp._pump_connection_core(stream, _Server(resp=RESP), "IO", act))
    assert stream.closed is True
    assert act.connections == 0  # incremented on entry, decremented in finally
    assert len(stream.sent) == 1
    assert json.loads(stream.sent[0].decode())["id"] == 1


def test_core_blank_and_invalid_lines():
    # blank line -> `if not line: continue`; invalid json -> send exc, continue
    stream = FakeStream(chunks=[b"\n", b"{not json}\n"])
    run(dp._pump_connection_core(stream, _Server(resp=None), "IO", dp._Activity()))
    assert stream.closed is True


def test_core_oversized_line_raises(monkeypatch):
    monkeypatch.setattr(dp, "_LINE_MAX_BYTES", 4)
    stream = FakeStream(chunks=[b"aaaaaaaa\n"])

    class Blocking:
        async def run(self, rs, ws, io):
            async with rs, ws:
                try:
                    await rs.receive()
                except anyio.EndOfStream:
                    pass
                await anyio.sleep_forever()  # cancelled once reader raises

    # anyio wraps a child-task (reader) exception in a BaseExceptionGroup.
    with pytest.raises(BaseExceptionGroup) as exc:
        run(dp._pump_connection_core(stream, Blocking(), "IO", dp._Activity()))
    runtime_errors = [e for e in exc.value.exceptions if isinstance(e, RuntimeError)]
    assert runtime_errors and "oversized JSON-RPC" in str(runtime_errors[0])


def test_core_reader_closed_resource():
    stream = FakeStream()

    async def raise_closed(n=65536):
        raise anyio.ClosedResourceError()

    stream.receive = raise_closed
    run(dp._pump_connection_core(stream, _Server(resp=None), "IO", dp._Activity()))
    assert stream.closed is True


def test_core_reader_receive_oserror():
    # OSError from stream.receive propagates past the inner EndOfStream-only
    # try and is swallowed by the shared `except (BrokenResourceError, OSError)`.
    stream = FakeStream()

    async def raise_oserror(n=65536):
        raise OSError("boom")

    stream.receive = raise_oserror
    run(dp._pump_connection_core(stream, _Server(resp=None), "IO", dp._Activity()))
    assert stream.closed is True


def test_core_writer_send_closed_resource():
    stream = FakeStream(chunks=[REQ_LINE], send_exc=anyio.ClosedResourceError())
    run(dp._pump_connection_core(stream, _Server(resp=RESP), "IO", dp._Activity()))
    assert stream.closed is True


def test_core_writer_send_oserror():
    stream = FakeStream(chunks=[REQ_LINE], send_exc=OSError("broken pipe"))
    run(dp._pump_connection_core(stream, _Server(resp=RESP), "IO", dp._Activity()))
    assert stream.closed is True


def test_core_aclose_oserror_swallowed():
    stream = FakeStream(chunks=[REQ_LINE], aclose_exc=OSError("nope"))
    run(dp._pump_connection_core(stream, _Server(resp=RESP), "IO", dp._Activity()))
    assert stream.closed is True


# --------------------------------------------------------------------------- #
# _idle_reaper
# --------------------------------------------------------------------------- #

def test_idle_reaper_exits_on_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(dp.os, "_exit", lambda c: (_ for _ in ()).throw(RuntimeError("EXIT")))
    (tmp_path / "sid.json").write_text("{}")  # session still present -> idle is the trigger
    act = dp._Activity()
    act.connections = 0
    act.last_active = time.monotonic() - 1000
    with pytest.raises(RuntimeError, match="EXIT"):
        run(dp._idle_reaper(act, 0.5, "sid", tmp_path))


def test_idle_reaper_exits_when_session_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(dp.os, "_exit", lambda c: (_ for _ in ()).throw(RuntimeError("EXIT")))
    act = dp._Activity()
    act.connections = 1  # busy -> not idle; session missing is the trigger
    act.last_active = time.monotonic()
    with pytest.raises(RuntimeError, match="EXIT"):
        run(dp._idle_reaper(act, 100.0, "missing-sid", tmp_path))


def test_idle_reaper_loops_then_exits(monkeypatch, tmp_path):
    # Covers the loop-back branch (condition False) before an exit iteration.
    monkeypatch.setattr(dp.os, "_exit", lambda c: (_ for _ in ()).throw(RuntimeError("EXIT")))

    async def noop_sleep(_s):
        return None

    monkeypatch.setattr(dp.anyio, "sleep", noop_sleep)
    tick = {"v": 0.0}
    monkeypatch.setattr(dp.time, "monotonic", lambda: (tick.__setitem__("v", tick["v"] + 1.0), tick["v"])[1])
    (tmp_path / "sid.json").write_text("{}")
    act = dp._Activity()
    act.connections = 0
    act.last_active = 0.0
    with pytest.raises(RuntimeError, match="EXIT"):
        run(dp._idle_reaper(act, 5.0, "sid", tmp_path))


# --------------------------------------------------------------------------- #
# _serve
# --------------------------------------------------------------------------- #

def _serve_config(tmp_path, transport):
    return {
        "sessions_dir": str(tmp_path),
        "args": ["-m", "json"],
        "state_path": str(tmp_path / "state.json"),
        "idle_timeout_seconds": 1.0,
        "session_id": "sid",
        "transport": transport,
        "fingerprint": "fp",
    }


def _patch_serve(monkeypatch, unix_called, tcp_called):
    monkeypatch.setattr(dp, "_build_mcp_server", lambda args: FakeServer())

    async def fake_reaper(*a, **k):
        return None

    monkeypatch.setattr(dp, "_idle_reaper", fake_reaper)

    async def fake_unix(cfg, sp, srv, io, act, tg):
        unix_called.append(True)

    async def fake_tcp(cfg, sp, srv, io, act, tg):
        tcp_called.append(True)

    monkeypatch.setattr(dp, "_serve_unix", fake_unix)
    monkeypatch.setattr(dp, "_serve_tcp", fake_tcp)


def test_serve_dispatches_unix(monkeypatch, tmp_path):
    unix_called, tcp_called = [], []
    _patch_serve(monkeypatch, unix_called, tcp_called)
    run(dp._serve(_serve_config(tmp_path, "unix")))
    assert unix_called and not tcp_called


def test_serve_dispatches_tcp(monkeypatch, tmp_path):
    unix_called, tcp_called = [], []
    _patch_serve(monkeypatch, unix_called, tcp_called)
    run(dp._serve(_serve_config(tmp_path, "tcp")))
    assert tcp_called and not unix_called


# --------------------------------------------------------------------------- #
# _serve_unix
# --------------------------------------------------------------------------- #

class _FakeListener:
    def __init__(self, streams):
        self._streams = list(streams)

    async def accept(self):
        if not self._streams:
            await anyio.sleep_forever()
        return self._streams.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_serve_unix(monkeypatch, tmp_path):
    listener = _FakeListener([FakeStream()])

    async def fake_create_unix_listener(path=None, **kw):
        return listener

    monkeypatch.setattr(dp.anyio, "create_unix_listener", fake_create_unix_listener)
    monkeypatch.setattr(dp.os, "chmod", lambda *a, **k: None)
    state_written = []
    monkeypatch.setattr(dp, "_write_state", lambda p, payload: state_written.append(payload))

    config = {"socket_path": str(tmp_path / "sock"), "fingerprint": "fp"}
    pumped = []

    async def driver():
        ready = anyio.Event()

        async def fake_pump(stream, srv, io, act):
            pumped.append(stream)
            ready.set()

        orig = dp._pump_connection
        dp._pump_connection = fake_pump
        try:
            async with anyio.create_task_group() as tg:
                await dp._serve_unix(
                    config, tmp_path / "state.json", FakeServer(), "IO", dp._Activity(), tg
                )
                await ready.wait()
                tg.cancel_scope.cancel()
        finally:
            dp._pump_connection = orig

    run(driver())
    assert len(pumped) == 1
    assert state_written[0]["transport"] == "unix"
    assert state_written[0]["socket_path"] == str(tmp_path / "sock")
    assert state_written[0]["ready"] is True


# --------------------------------------------------------------------------- #
# _serve_tcp
# --------------------------------------------------------------------------- #

class _FakeTcpListener:
    def __init__(self, streams):
        self._streams = list(streams)

    def extra(self, attr):
        return ("127.0.0.1", 54321)

    async def accept(self):
        if not self._streams:
            await anyio.sleep_forever()
        return self._streams.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeMultiListener:
    def __init__(self, listener):
        self.listeners = [listener]


def test_serve_tcp(monkeypatch, tmp_path):
    tcp_listener = _FakeTcpListener([FakeStream()])

    async def fake_create_tcp_listener(local_host=None, local_port=0):
        return _FakeMultiListener(tcp_listener)

    monkeypatch.setattr(dp.anyio, "create_tcp_listener", fake_create_tcp_listener)
    state_written = []
    monkeypatch.setattr(dp, "_write_state", lambda p, payload: state_written.append(payload))

    config = {"fingerprint": "fp"}
    pumped = []

    async def driver():
        ready = anyio.Event()

        async def fake_pump_tcp(stream, srv, io, act, secret):
            pumped.append(secret)
            ready.set()

        orig = dp._pump_tcp_connection
        dp._pump_tcp_connection = fake_pump_tcp
        try:
            async with anyio.create_task_group() as tg:
                await dp._serve_tcp(
                    config, tmp_path / "state.json", FakeServer(), "IO", dp._Activity(), tg
                )
                await ready.wait()
                tg.cancel_scope.cancel()
        finally:
            dp._pump_tcp_connection = orig

    run(driver())
    assert len(pumped) == 1 and pumped[0]  # connect_secret passed through
    payload = state_written[0]
    assert payload["transport"] == "tcp"
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 54321
    assert "connect_secret" in payload
    assert payload["ready"] is True


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def test_main_usage(monkeypatch, capsys):
    monkeypatch.setattr(dp.sys, "argv", ["daemon_process.py"])
    assert dp.main() == 2
    assert "usage" in capsys.readouterr().err


def test_main_runs(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dp.sys, "argv", ["daemon_process.py", str(cfg)])
    captured = {}
    monkeypatch.setattr(dp.anyio, "run", lambda coro, *args: captured.update(coro=coro, args=args))
    assert dp.main() == 0
    assert captured["coro"] is dp._serve


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(0)])
def test_main_handles_interrupt(monkeypatch, tmp_path, exc):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dp.sys, "argv", ["daemon_process.py", str(cfg)])

    def raise_exc(coro, *args):
        raise exc

    monkeypatch.setattr(dp.anyio, "run", raise_exc)
    assert dp.main() == 0

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="ba-shortcut-gate-")
os.environ["BETTER_AGENT_HOME"] = _HOME
_BACKEND = str(Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import shortcut_rate_limit as gate


class _ProviderHandler(BaseHTTPRequestHandler):
    calls = 0
    status = 429
    lock = threading.Lock()

    def do_POST(self) -> None:
        with self.lock:
            type(self).calls += 1
        body = b'{"content":[{"text":"[0]"}]}'
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        if type(self).status == 429:
            self.send_header("retry-after", "30")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def _claim_worker(home: str, provider_base: str, start, results) -> None:
    os.environ["BETTER_AGENT_HOME"] = home
    from paths import reset_home_cache
    reset_home_cache()
    import shortcut_rate_limit
    scope = shortcut_rate_limit.scope_key(
        provider_id="provider",
        base_url=provider_base,
        model="model",
        api_key="credential-one",
    )
    start.wait()
    claim = shortcut_rate_limit.claim(scope)
    status = 0
    if claim.lease:
        request = urllib.request.Request(f"{provider_base}/v1/messages", data=b"{}", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                status = response.status
            shortcut_rate_limit.finish(claim.lease)
        except urllib.error.HTTPError as exc:
            status = exc.code
            shortcut_rate_limit.finish(
                claim.lease,
                cooldown_secs=shortcut_rate_limit.retry_after_seconds(exc.headers.get("retry-after")),
            )
    results.put((scope, claim.reason, status))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    provider_base = f"http://127.0.0.1:{server.server_port}"
    scope = gate.scope_key(
        provider_id="provider",
        base_url="HTTPS://Example.COM:443/api/",
        model="model",
        api_key="credential-one",
    )
    equivalent = gate.scope_key(
        provider_id="provider",
        base_url="https://example.com/api",
        model="model",
        api_key="credential-one",
    )
    isolated = gate.scope_key(
        provider_id="provider",
        base_url="https://example.com/api",
        model="model",
        api_key="credential-two",
    )
    _assert(scope == equivalent, "endpoint normalization changed scope")
    _assert(scope != isolated, "credentials were not isolated")
    for invalid_scope in ("../escape", "a" * 63, "A" * 64, "g" * 64):
        try:
            gate.claim(invalid_scope)
            raise AssertionError(f"invalid scope was accepted: {invalid_scope!r}")
        except ValueError:
            pass
    _assert(not (Path(_HOME) / "runtime" / "escape.json").exists(), "scope escaped state root")
    http_scope = gate.scope_key(
        provider_id="provider",
        base_url=provider_base,
        model="model",
        api_key="credential-one",
    )

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_claim_worker, args=(_HOME, provider_base, start, results))
        for _ in range(8)
    ]
    for worker in workers:
        worker.start()
    start.set()
    worker_results = [results.get(timeout=5) for _ in workers]
    _assert({item[0] for item in worker_results} == {http_scope}, "processes derived different scopes")
    reasons = [item[1] for item in worker_results]
    for worker in workers:
        worker.join(timeout=5)
        _assert(worker.exitcode == 0, f"worker failed: {worker.exitcode}")
    _assert(reasons.count("probe") == 1, f"multiple upstream probes admitted: {reasons}")
    _assert(set(reasons) <= {"probe", "inflight", "cooldown"}, f"unexpected claims: {reasons}")
    _assert(_ProviderHandler.calls == 1, f"provider received {_ProviderHandler.calls} calls")
    _assert([item[2] for item in worker_results].count(429) == 1, "probe did not observe 429")
    _assert(gate.claim(http_scope).reason == "cooldown", "shared cooldown was not persisted")
    future = time.time() + 31
    expired = gate.claim(http_scope, now=future)
    _assert(expired.lease is not None, "fake-clock cooldown expiry did not admit probe")
    _ProviderHandler.status = 200
    request = urllib.request.Request(f"{provider_base}/v1/messages", data=b"{}", method="POST")
    with urllib.request.urlopen(request, timeout=2) as response:
        _assert(response.status == 200, "post-cooldown probe did not succeed")
    gate.finish(expired.lease, now=future)
    _assert(_ProviderHandler.calls == 2, "post-cooldown probe call count was not one")

    _assert(gate.retry_after_seconds("3") == 3, "delta Retry-After was not parsed")
    _assert(gate.retry_after_seconds("99999") == 900, "Retry-After was not bounded")
    _assert(gate.retry_after_seconds("bad") == 60, "malformed Retry-After did not default")
    _assert(
        gate.retry_after_seconds("Thu, 01 Jan 1970 00:02:00 GMT", now=60) == 60,
        "HTTP-date Retry-After was not parsed",
    )

    crash_scope = gate.scope_key(
        provider_id="crash", base_url="https://example.com", model="model", api_key="key"
    )
    first = gate.claim(crash_scope, now=100)
    _assert(first.lease is not None, "initial lease missing")
    recovered = gate.claim(crash_scope, now=131)
    _assert(recovered.lease is not None and recovered.recovered, "expired lease was not recovered")
    gate.finish(recovered.lease, now=131)

    corrupt_scope = gate.scope_key(
        provider_id="corrupt", base_url="https://example.com", model="model", api_key="key"
    )
    state_path, _ = gate._paths(corrupt_scope)
    state_path.write_text("not-json", encoding="utf-8")
    corrupted = gate.claim(corrupt_scope, now=200)
    _assert(corrupted.lease is not None and corrupted.corrupt, "corrupt projection did not rebuild")
    gate.finish(corrupted.lease, cooldown_secs=2, now=200)
    _assert(gate.claim(corrupt_scope, now=199).reason == "cooldown", "clock rollback bypassed cooldown")
    _assert((state_path.stat().st_mode & 0o777) == 0o600, "state file permissions are not private")

    rollback_scope = gate.scope_key(
        provider_id="rollback", base_url="https://example.com", model="model", api_key="key"
    )
    rollback = gate.claim(rollback_scope, now=10_000)
    _assert(rollback.lease is not None, "rollback lease missing")
    gate.finish(rollback.lease, cooldown_secs=30, now=10_000)
    _assert(gate.claim(rollback_scope, now=100).reason == "cooldown", "large rollback lost cooldown")
    _assert(gate.claim(rollback_scope, now=131).lease is not None, "large rollback became indefinite")

    small_rollback_scope = gate.scope_key(
        provider_id="small-rollback", base_url="https://example.com", model="model", api_key="key"
    )
    small_rollback = gate.claim(small_rollback_scope, now=1_000)
    _assert(small_rollback.lease is not None, "small rollback lease missing")
    gate.finish(small_rollback.lease, cooldown_secs=30, now=1_000)
    _assert(
        gate.claim(small_rollback_scope, now=995).reason == "cooldown",
        "small rollback lost cooldown",
    )
    _assert(
        gate.claim(small_rollback_scope, now=1_026).lease is not None,
        "small rollback became indefinite",
    )

    sync_calls = []
    real_sync = gate._fsync_parent
    gate._fsync_parent = lambda path: (sync_calls.append(path), real_sync(path))[1]
    durable_scope = gate.scope_key(
        provider_id="durable", base_url="https://example.com", model="model", api_key="key"
    )
    durable = gate.claim(durable_scope, now=300)
    _assert(durable.lease is not None and sync_calls, "atomic replace did not fsync parent")
    gate.finish(durable.lease, now=300)
    gate._fsync_parent = real_sync

    root = gate._root()
    _assert((root.stat().st_mode & 0o777) == 0o700, "gate directory permissions are not private")
    for path in root.iterdir():
        if path.is_file():
            _assert((path.stat().st_mode & 0o777) == 0o600, f"non-private mode: {path.name}")

    disk = json.dumps([p.read_text(errors="ignore") for p in Path(_HOME).rglob("*") if p.is_file()])
    _assert("credential-one" not in disk and "credential-two" not in disk, "credential leaked to state")
    server.shutdown()
    server.server_close()
    print("PASS shortcut rate-limit gate is cross-process and crash-safe")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)

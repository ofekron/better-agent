#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.anyio

TEST_HOME = tempfile.mkdtemp(prefix="ba-codex-headless-")
os.environ["BETTER_AGENT_HOME"] = TEST_HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_headless_execution import (  # noqa: E402
    PreparedCodexHeadless,
    execute_codex_headless,
    prepare_codex_headless,
)
from headless_request_contract import (  # noqa: E402
    AdmittedHeadlessRequest,
    HeadlessAuthority,
    HeadlessRequest,
    admit_headless_request,
)
import node_rpc_handlers  # noqa: E402
import config_store  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from provider_fugu import FuguProvider  # noqa: E402


@pytest.fixture
def root() -> Path:
    return Path(TEST_HOME)


@pytest.fixture
def fake(root: Path) -> Path:
    return _fake_codex(root)


def _fake_codex(root: Path) -> Path:
    path = root / "codex"
    path.write_text(
        f"#!{sys.executable}\n" + """
import json, os, sys
log = os.environ.get("HEADLESS_TEST_LOG")
for raw in sys.stdin:
    msg = json.loads(raw)
    method = msg.get("method")
    if method == "initialized":
        continue
    if log:
        with open(log, "a", encoding="utf-8") as out:
            out.write(json.dumps({"argv": sys.argv[1:], "method": method, "params": msg.get("params"), "sakana": bool(os.environ.get("SAKANA_API_KEY"))}) + "\\n")
    rid = msg.get("id")
    if method == "initialize":
        print(json.dumps({"id": rid, "result": {}}), flush=True)
    elif method in ("thread/start", "thread/resume", "thread/fork"):
        suffix = "forked" if method == "thread/fork" else "fresh"
        print(json.dumps({"id": rid, "result": {"thread": {"id": "thread-" + suffix}}}), flush=True)
    elif method == "turn/start":
        print(json.dumps({"id": rid, "result": {}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "precise-result"}}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"turn": {"status": "completed", "usage": {"inputTokens": 3, "outputTokens": 2}}}}), flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _record(root: Path, kind: str) -> dict:
    config = root / f"{kind}-home"
    config.mkdir(exist_ok=True)
    (config / "config.toml").write_text("", encoding="utf-8")
    return {
        "id": f"{kind}-provider",
        "kind": kind,
        "generation": str(uuid.uuid4()),
        "revision": 4,
        "execution_revision": 2,
        "mode": "api_key" if kind == "fugu" else "subscription",
        "config_dir": str(config),
        "runner": "native",
    }


def _admitted(
    record: dict,
    *,
    fork: bool,
    resume: bool = False,
    no_tools: bool = False,
    model: str | None = None,
):
    selected_model = model or ("fugu" if record["kind"] == "fugu" else "gpt-5")
    owner = {"kind": "session", "id": "session-1"}
    request = HeadlessRequest.from_dict({
        "schema": 1,
        "prompt": "be precise",
        "owner": owner,
        "fork": fork,
        "no_tools": no_tools,
        "timeout": 10,
    })
    authority = HeadlessAuthority.create(
        owner_kind="session",
        owner_id="session-1",
        provider={
            key: record[key]
            for key in ("id", "kind", "generation", "execution_revision")
        },
        model=selected_model,
        reasoning_effort="high",
        runner="native",
        permission_scope="spawn_runs",
        routing={"node_id": "primary"},
        cwd=str(Path(TEST_HOME)),
        resume_sid="thread-parent" if fork or resume else None,
        supports_fork=True,
        supports_no_tools=True,
    )
    return admit_headless_request(request, authority)


async def _exercise(
    provider,
    record: dict,
    fake: Path,
    *,
    fork: bool,
    resume: bool = False,
    model: str | None = None,
) -> tuple[dict, list[dict]]:
    log = Path(TEST_HOME) / f"{record['kind']}-{uuid.uuid4().hex}.jsonl"
    admitted = _admitted(record, fork=fork, resume=resume, model=model)
    prepared = prepare_codex_headless(provider, admitted, launcher_path=str(fake))
    runtime = dict(record)
    runtime["api_key"] = "sakana-secret" if record["kind"] == "fugu" else ""
    runtime["_credential_authoritative"] = record["kind"] == "fugu"
    runtime["_credential_status"] = (
        "available" if record["kind"] == "fugu" else "missing"
    )
    class Hydration:
        def runtime_record(self) -> dict:
            return runtime

    original_get_provider = node_rpc_handlers.get_provider
    original_hydrate = config_store.hydrate_provider_execution
    original_path = os.environ.get("PATH", "")
    node_rpc_handlers.get_provider = lambda *_args: provider

    def hydrate(provider_id: str, **expected) -> Hydration:
        assert provider_id == record["id"]
        assert expected == {
            "expected_generation": record["generation"],
            "expected_execution_revision": record["execution_revision"],
        }
        return Hydration()

    config_store.hydrate_provider_execution = hydrate
    os.environ["PATH"] = f"{fake.parent}{os.pathsep}{original_path}"
    os.environ["HEADLESS_TEST_LOG"] = str(log)
    try:
        response = await node_rpc_handlers._rpc_run_admitted_headless({
            "admitted": admitted.to_dict(),
        })
        result = response["result"]
    finally:
        node_rpc_handlers.get_provider = original_get_provider
        config_store.hydrate_provider_execution = original_hydrate
        os.environ["PATH"] = original_path
        os.environ.pop("HEADLESS_TEST_LOG", None)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert "sakana-secret" not in json.dumps(prepared.to_dict())
    assert "sakana-secret" not in json.dumps(result)
    return result, rows


async def test_codex_and_fugu_exact_execution(root: Path, fake: Path) -> None:
    codex_record = _record(root, "codex")
    codex_result, codex_rows = await _exercise(
        CodexProvider(codex_record), codex_record, fake, fork=False,
    )
    assert codex_result["result"] == "precise-result"
    assert codex_result["session_id"] == "thread-fresh"
    assert next(row for row in codex_rows if row["method"] == "turn/start")["params"]["model"] == "gpt-5"
    assert next(row for row in codex_rows if row["method"] == "turn/start")["params"]["sandboxPolicy"]["type"] == "dangerFullAccess"
    resume_result, resume_rows = await _exercise(
        CodexProvider(codex_record),
        codex_record,
        fake,
        fork=False,
        resume=True,
    )
    assert resume_result["session_id"] == "thread-fresh"
    resume_row = next(
        row for row in resume_rows if row["method"] == "thread/resume"
    )
    assert resume_row["params"]["threadId"] == "thread-parent"

    fugu_record = _record(root, "fugu")
    fugu_result, fugu_rows = await _exercise(
        FuguProvider(fugu_record), fugu_record, fake, fork=True,
    )
    assert fugu_result["session_id"] == "thread-forked"
    fork_row = next(row for row in fugu_rows if row["method"] == "thread/fork")
    assert fork_row["params"]["threadId"] == "thread-parent"
    argv = fugu_rows[0]["argv"]
    assert 'model_provider="sakana"' in argv
    assert 'model="fugu"' in argv
    assert all(row["sakana"] for row in fugu_rows)


async def test_codex_delegates_model_validation_to_cli(
    root: Path,
    fake: Path,
) -> None:
    record = _record(root, "codex")
    model = "codex-model-not-in-better-agent-catalog"
    result, rows = await _exercise(
        CodexProvider(record),
        record,
        fake,
        fork=False,
        model=model,
    )
    assert result["result"] == "precise-result"
    turn = next(row for row in rows if row["method"] == "turn/start")
    assert turn["params"]["model"] == model


async def test_rejections_and_drift(root: Path, fake: Path) -> None:
    record = _record(root, "codex")
    provider = CodexProvider(record)
    assert provider.supports_headless_no_tools is False
    assert FuguProvider(_record(root, "fugu")).supports_headless_no_tools is False
    try:
        prepare_codex_headless(
            provider,
            _admitted(record, fork=False, no_tools=True),
            launcher_path=str(fake),
        )
    except ValueError as exc:
        assert "no-tools" in str(exc)
    else:
        raise AssertionError("expected no-tools rejection")
    hydration_calls = 0
    original_hydrate = config_store.hydrate_provider_execution

    def hydration_forbidden(*_args, **_kwargs):
        nonlocal hydration_calls
        hydration_calls += 1
        raise AssertionError("no-tools rejection must precede credential hydration")

    config_store.hydrate_provider_execution = hydration_forbidden
    try:
        try:
            await provider.run_admitted_headless(
                _admitted(record, fork=False, no_tools=True),
            )
        except ValueError as exc:
            assert "no-tools" in str(exc)
        else:
            raise AssertionError("no-tools execution must fail closed")
    finally:
        config_store.hydrate_provider_execution = original_hydrate
    assert hydration_calls == 0

    try:
        await node_rpc_handlers._rpc_run_admitted_headless({
            "admitted": _admitted(record, fork=False).to_dict(),
            "api_key": "forbidden",
        })
    except ValueError as exc:
        assert "envelope" in str(exc)
    else:
        raise AssertionError("RPC authority override must fail")

    tampered = _admitted(record, fork=False).to_dict()
    tampered["provider"]["execution_revision"] += 1
    malformed = _admitted(record, fork=False).to_dict()
    malformed["provider"]["execution_revision"] = "2"
    with pytest.MonkeyPatch.context() as rpc_patch:
        rpc_patch.setattr(node_rpc_handlers, "get_provider", lambda *_args: provider)
        try:
            await node_rpc_handlers._rpc_run_admitted_headless({
                "admitted": tampered,
            })
        except ValueError as exc:
            assert "authority" in str(exc)
        else:
            raise AssertionError("RPC execution authority drift must fail")
        try:
            await node_rpc_handlers._rpc_run_admitted_headless({
                "admitted": malformed,
            })
        except ValueError as exc:
            assert "provider authority" in str(exc)
        else:
            raise AssertionError("RPC malformed authority must fail")

    fugu_record = _record(root, "fugu")
    try:
        prepare_codex_headless(
            FuguProvider(fugu_record),
            _admitted(fugu_record, fork=False, model="not-fugu"),
            launcher_path=str(fake),
        )
    except ValueError as exc:
        assert "Fugu model" in str(exc)
    else:
        raise AssertionError("Fugu curated model validation must remain")

    admitted = _admitted(record, fork=False)
    metadata_only_drift = dict(record)
    metadata_only_drift["revision"] += 1

    def local_codec_forbidden(*_args, **_kwargs):
        raise AssertionError("local Codex headless execution must stay typed")

    with pytest.MonkeyPatch.context() as local_patch:
        local_patch.setattr(
            AdmittedHeadlessRequest,
            "to_dict",
            local_codec_forbidden,
        )
        local_patch.setattr(
            AdmittedHeadlessRequest,
            "from_dict",
            classmethod(local_codec_forbidden),
        )
        local_patch.setattr(
            PreparedCodexHeadless,
            "to_dict",
            local_codec_forbidden,
        )
        local_patch.setattr(
            PreparedCodexHeadless,
            "from_dict",
            classmethod(local_codec_forbidden),
        )
        prepared = prepare_codex_headless(
            provider,
            admitted,
            launcher_path=str(fake),
        )
        result = await execute_codex_headless(
            provider,
            prepared,
            runtime_record=metadata_only_drift,
        )
    assert result["result"] == "precise-result"

    prepared_wire = prepared.to_dict()
    prepared_wire["admitted"]["unexpected"] = True
    try:
        PreparedCodexHeadless.from_dict(prepared_wire)
    except ValueError as exc:
        assert "admitted headless request" in str(exc)
    else:
        raise AssertionError("process codec must reject unknown admission fields")

    execution_drift = dict(record)
    execution_drift["execution_revision"] += 1
    try:
        await execute_codex_headless(
            provider,
            prepared,
            runtime_record=execution_drift,
        )
    except RuntimeError as exc:
        assert "authority" in str(exc)
    else:
        raise AssertionError("execution revision drift must fail")

    (Path(record["config_dir"]) / "config.toml").write_text("changed=true\n", encoding="utf-8")
    try:
        await execute_codex_headless(provider, prepared, runtime_record=record)
    except RuntimeError as exc:
        assert "authority" in str(exc)
    else:
        raise AssertionError("config drift must fail")


async def main_async() -> None:
    root = Path(TEST_HOME)
    fake = _fake_codex(root)
    await test_codex_and_fugu_exact_execution(root, fake)
    await test_codex_delegates_model_validation_to_cli(root, fake)
    await test_rejections_and_drift(root, fake)


def main() -> None:
    try:
        asyncio.run(main_async())
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print("Codex/Fugu admitted headless integration: ok")


if __name__ == "__main__":
    main()

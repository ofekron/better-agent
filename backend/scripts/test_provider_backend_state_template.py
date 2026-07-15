#!/usr/bin/env python3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider import Provider
from provider_agy import AgyProvider
from provider_amp import AmpProvider
from provider_claude import CLAUDE_INGESTION_VERSION, ClaudeProvider
from provider_codex import CODEX_INGESTION_VERSION, CodexProvider
from provider_copilot import CopilotProvider
from provider_cursor import CursorProvider
from provider_fugu import FuguProvider
from provider_gemini import GeminiProvider
from provider_kimi import KimiProvider
from provider_opencode import OpencodeProvider
from provider_openai import OPENAI_INGESTION_VERSION, OpenAIProvider
from provider_pi import PiProvider
from provider_qwen import QwenProvider
from provider_remote import RemoteProviderProxy


def common_run(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-1",
        run_dir=tmp_path,
        app_session_id="session-1",
        persist_to=None,
        mode="native",
        popen=SimpleNamespace(pid=1234),
        started_at="2026-07-14T00:00:00",
        session_id="native-1",
        cancelled=False,
        target_message_id="message-1",
        turn_run_id="turn-1",
        lifecycle_msg_id="lifecycle-1",
    )


def test_common_schema() -> None:
    owner = SimpleNamespace(
        id="provider-1",
        _backend_state_path=lambda rs: rs.run_dir / "backend_state.json",
    )
    run = common_run(Path("/tmp/run-1"))
    state = Provider._common_backend_state(owner, run, processed_line=7)
    assert state == {
        "run_id": "run-1",
        "app_session_id": "session-1",
        "persist_to": "session-1",
        "mode": "native",
        "runner_pid": 1234,
        "started_at": "2026-07-14T00:00:00",
        "session_id": "native-1",
        "cancelled": False,
        "target_message_id": "message-1",
        "turn_run_id": "turn-1",
        "lifecycle_msg_id": "lifecycle-1",
        "provider_id": "provider-1",
        "processed_line": 7,
    }
    try:
        Provider._common_backend_state(owner, run, run_id="override")
    except ValueError:
        pass
    else:
        raise AssertionError("provider extras must not override common fields")


def test_template_sequence_and_opt_out() -> None:
    calls: list[str] = []
    owner = SimpleNamespace(
        _persists_backend_state=lambda _rs: True,
        _backend_state_fields=lambda _rs: calls.append("fields") or {"cursor": 8},
        _common_backend_state=lambda _rs, **fields: (
            calls.append("serialize") or {"serialized": fields}
        ),
        _persist_backend_state=lambda _rs, _data: calls.append("persist"),
    )
    Provider._write_backend_state(owner, object())
    assert calls == ["fields", "serialize", "persist"]

    skipped = SimpleNamespace(
        _persists_backend_state=lambda _rs: False,
        _backend_state_fields=lambda _rs: (_ for _ in ()).throw(
            AssertionError("opt-out evaluated fields")
        ),
        _common_backend_state=lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("opt-out serialized")
        ),
        _persist_backend_state=lambda *_a: (_ for _ in ()).throw(
            AssertionError("opt-out persisted")
        ),
    )
    Provider._write_backend_state(skipped, object())


def test_atomic_write_precedes_discovery_and_failure_stops_discovery(tmp_path: Path) -> None:
    owner = SimpleNamespace(
        id="provider-1",
        _backend_state_path=lambda rs: rs.run_dir / "backend_state.json",
    )
    run = common_run(tmp_path)
    calls: list[str] = []
    with (
        patch("runs_dir.atomic_write_json", side_effect=lambda *_a: calls.append("write")),
        patch("spawn_ledger.record_discovered", side_effect=lambda *_a: calls.append("discover")),
    ):
        Provider._persist_backend_state(owner, run, {"valid": True})
    assert calls == ["write", "discover"]

    calls.clear()
    with (
        patch("runs_dir.atomic_write_json", side_effect=OSError("disk failed")),
        patch("spawn_ledger.record_discovered", side_effect=lambda *_a: calls.append("discover")),
    ):
        try:
            Provider._persist_backend_state(owner, run, {"valid": True})
        except OSError:
            pass
        else:
            raise AssertionError("write failure was swallowed")
    assert calls == []


def test_template_method_is_final() -> None:
    for cls in (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        OpenAIProvider,
        RemoteProviderProxy,
        AgyProvider,
        AmpProvider,
        CopilotProvider,
        CursorProvider,
        FuguProvider,
        KimiProvider,
        OpencodeProvider,
        PiProvider,
        QwenProvider,
    ):
        assert cls._write_backend_state is Provider._write_backend_state

    try:
        class InvalidProvider(Provider):
            def _write_backend_state(self, rs):
                return None
    except TypeError:
        pass
    else:
        raise AssertionError("Provider allowed template override")


def test_provider_cursor_fields(tmp_path: Path) -> None:
    run = common_run(tmp_path)
    jsonl_path = tmp_path / "provider.jsonl"
    jsonl_path.write_text("{}\n", encoding="utf-8")

    run.jsonl_path = jsonl_path
    run.applied_byte = 11
    run.root_id = "root-1"
    run.cwd = "/repo"
    claude = ClaudeProvider._backend_state_fields(object(), run)
    assert claude == {
        "jsonl_path": str(jsonl_path),
        "processed_byte": 11,
        "jsonl_inode": jsonl_path.stat().st_ino,
        "root_id": "root-1",
        "cwd": "/repo",
        "ingestion_version": CLAUDE_INGESTION_VERSION,
    }

    run.processed_line = 13
    run.applied_byte_offset = 17
    run.child_sources = {"child": 19}
    codex = CodexProvider._backend_state_fields(object(), run)
    assert codex == {
        "jsonl_path": str(jsonl_path),
        "processed_line": 13,
        "processed_byte_offset": 17,
        "ingestion_version": CODEX_INGESTION_VERSION,
        "child_sources": {"child": 19},
    }

    run.applied_line = 23
    gemini = GeminiProvider._backend_state_fields(object(), run)
    assert gemini == {
        "jsonl_path": str(tmp_path / "session_events.jsonl"),
        "processed_line": 23,
    }

    openai = OpenAIProvider._backend_state_fields(
        SimpleNamespace(KIND="openai"), run
    )
    assert openai == {
        "jsonl_path": str(tmp_path / "session_events.jsonl"),
        "processed_line": 23,
        "provider_kind": "openai",
        "ingestion_version": OPENAI_INGESTION_VERSION,
    }


def test_claude_inode_failure_is_nonfatal(tmp_path: Path) -> None:
    class BrokenPath:
        def exists(self):
            return True

        def stat(self):
            raise OSError("gone")

        def __str__(self):
            return "/tmp/gone.jsonl"

    run = common_run(tmp_path)
    run.jsonl_path = BrokenPath()
    run.applied_byte = 5
    run.root_id = "root-1"
    run.cwd = "/repo"
    fields = ClaudeProvider._backend_state_fields(object(), run)
    assert fields["jsonl_inode"] is None
    assert fields["processed_byte"] == 5


def test_inherited_families_reuse_parent_hooks() -> None:
    assert FuguProvider._backend_state_fields is CodexProvider._backend_state_fields
    for cls in (
        AgyProvider,
        AmpProvider,
        CopilotProvider,
        CursorProvider,
        KimiProvider,
        OpencodeProvider,
        PiProvider,
        QwenProvider,
    ):
        assert cls._backend_state_fields is GeminiProvider._backend_state_fields


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory(prefix="provider-template-") as tmp:
        path = Path(tmp)
        test_common_schema()
        test_template_sequence_and_opt_out()
        test_atomic_write_precedes_discovery_and_failure_stops_discovery(path)
        test_template_method_is_final()
        test_provider_cursor_fields(path)
        test_claude_inode_failure_is_nonfatal(path)
        test_inherited_families_reuse_parent_hooks()
    print("provider backend-state template tests passed")

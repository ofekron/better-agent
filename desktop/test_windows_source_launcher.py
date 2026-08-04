from __future__ import annotations

import os
from pathlib import Path

import pytest

import windows_source_launcher


class FakeSupervisor:
    def __init__(
        self,
        state_root: Path,
        *,
        request_content: str | None = None,
        wrong_generation: bool = False,
    ) -> None:
        self.state_root = state_root
        self.request_content = request_content
        self.wrong_generation = wrong_generation
        self.starts = 0
        self.shutdown_called = False
        self.generation_id = ""
        self.signals = 0

    def handle(self, request):
        if request["op"] == "start":
            self.starts += 1
            self.generation_id = f"{self.starts:032x}"
            if self.request_content is not None:
                path = self.state_root / "restart_requested"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self.request_content, encoding="utf-8")
                os.utime(path, (2, 2))
            return {"pid": 1000 + self.starts, "generation_id": self.generation_id}
        if request["op"] == "status":
            return {
                "terminal": True,
                "generation_id": (
                    "f" * 32 if self.wrong_generation else self.generation_id
                ),
                "returncode": 1,
            }
        if request["op"] == "signal":
            self.signals += 1
            return {"ok": True}
        raise AssertionError(request)

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_never_ready_generations_open_circuit_and_cleanup(tmp_path) -> None:
    supervisor = FakeSupervisor(tmp_path)

    result = windows_source_launcher.run(
        tmp_path,
        "127.0.0.1",
        8000,
        supervisor=supervisor,
        state_root=tmp_path,
        monotonic=lambda: 10_000,
        wall_time=lambda: 1,
        health_probe=lambda _: False,
        install_signal_handlers=False,
        activate_dependency_checkout=lambda path: path,
    )

    assert result == 1
    assert supervisor.starts == 6
    assert supervisor.shutdown_called is True


def test_invalid_restart_intent_fails_closed_and_cleans_up(tmp_path) -> None:
    supervisor = FakeSupervisor(tmp_path, request_content="contains/slash")

    with pytest.raises(OSError):
        windows_source_launcher.run(
            tmp_path,
            "127.0.0.1",
            8000,
            supervisor=supervisor,
            state_root=tmp_path,
            monotonic=lambda: 1,
            wall_time=lambda: 1,
            health_probe=lambda _: False,
            install_signal_handlers=False,
            activate_dependency_checkout=lambda path: path,
        )

    assert supervisor.starts == 1
    assert supervisor.shutdown_called is True


def test_terminal_generation_must_match_and_cleanup(tmp_path) -> None:
    supervisor = FakeSupervisor(tmp_path, wrong_generation=True)

    with pytest.raises(RuntimeError):
        windows_source_launcher.run(
            tmp_path,
            "127.0.0.1",
            8000,
            supervisor=supervisor,
            state_root=tmp_path,
            monotonic=lambda: 1,
            wall_time=lambda: 1,
            health_probe=lambda _: False,
            install_signal_handlers=False,
            activate_dependency_checkout=lambda path: path,
        )

    assert supervisor.shutdown_called is True


def test_missing_primary_attestation_reverts_generation(tmp_path, monkeypatch) -> None:
    class RestartObserved(RuntimeError):
        pass

    class AttestationSupervisor(FakeSupervisor):
        def handle(self, request):
            if request["op"] == "start" and self.signals:
                raise RestartObserved
            if request["op"] == "status" and self.signals == 0:
                return {
                    "terminal": False,
                    "generation_id": self.generation_id,
                    "returncode": None,
                }
            return super().handle(request)

    supervisor = AttestationSupervisor(tmp_path)
    states = iter([0, 100, 100, 100, 100, 100, 100])
    finalized: list[Path] = []
    expired: list[Path] = []

    monkeypatch.setattr(
        windows_source_launcher,
        "_ready",
        lambda _: True,
    )
    import repository_alignment

    monkeypatch.setattr(
        repository_alignment,
        "finalize_node_activation",
        lambda path: finalized.append(path) or "pending",
    )
    monkeypatch.setattr(
        repository_alignment,
        "expire_node_activation",
        lambda path: expired.append(path) or True,
    )

    with pytest.raises(RestartObserved):
        windows_source_launcher.run(
            tmp_path,
            "127.0.0.1",
            8000,
            supervisor=supervisor,
            state_root=tmp_path,
            monotonic=lambda: next(states, 100),
            wall_time=lambda: 0,
            health_probe=lambda _: True,
            install_signal_handlers=False,
            primary_attestation_timeout_seconds=10,
            activate_dependency_checkout=lambda path: path,
        )

    assert finalized
    assert expired
    assert supervisor.signals >= 1


def test_activation_failure_prevents_backend_spawn(tmp_path, monkeypatch) -> None:
    supervisor = FakeSupervisor(tmp_path)
    state = {"active": str(tmp_path), "status": "active", "request_id": ""}
    from daemonhost import pointer

    monkeypatch.setattr(pointer, "read", lambda: dict(state))
    monkeypatch.setattr(pointer, "resolve", lambda _default: str(tmp_path))
    monkeypatch.setattr(pointer, "revert_if_switching", lambda *_args: False)

    with pytest.raises(RuntimeError, match="activation failed"):
        windows_source_launcher.run(
            tmp_path,
            "127.0.0.1",
            8000,
            supervisor=supervisor,
            state_root=tmp_path,
            install_signal_handlers=False,
            activate_dependency_checkout=lambda _path: (_ for _ in ()).throw(
                RuntimeError("activation failed")
            ),
        )

    assert supervisor.starts == 0
    assert supervisor.shutdown_called is True


def test_activation_failure_reverts_only_matching_switch(tmp_path, monkeypatch) -> None:
    state = {
        "active": str(tmp_path),
        "status": "switching",
        "request_id": "request-1",
    }
    calls: list[tuple[str, str]] = []
    from daemonhost import pointer

    monkeypatch.setattr(pointer, "read", lambda: dict(state))

    def revert(reason: str, request_id: str) -> bool:
        calls.append((reason, request_id))
        return request_id == state["request_id"]

    monkeypatch.setattr(pointer, "revert_if_switching", revert)

    result = windows_source_launcher._activate_before_start(
        tmp_path,
        dict(state),
        lambda _path: (_ for _ in ()).throw(RuntimeError("activation failed")),
    )

    assert result is False
    assert calls == [("target dependency activation failed", "request-1")]


def test_pointer_change_during_activation_prevents_backend_spawn(
    tmp_path,
    monkeypatch,
) -> None:
    before = {"active": str(tmp_path), "status": "active", "request_id": "one"}
    after = {**before, "request_id": "two"}
    from daemonhost import pointer

    monkeypatch.setattr(pointer, "read", lambda: dict(after))

    assert windows_source_launcher._activate_before_start(
        tmp_path,
        before,
        lambda path: path,
    ) is False

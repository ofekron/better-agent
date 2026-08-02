from __future__ import annotations

import sys

import pytest

from resilient_stdio import _ResilientTextStream, protect_standard_streams


class FakeStream:
    def __init__(
        self,
        *,
        write_exc: BaseException | None = None,
        flush_exc: BaseException | None = None,
    ) -> None:
        self.written: list[str] = []
        self.flush_count = 0
        self._write_exc = write_exc
        self._flush_exc = flush_exc
        self.name = "fake"

    def write(self, value: str) -> int:
        if self._write_exc is not None:
            raise self._write_exc
        self.written.append(value)
        return len(value)

    def flush(self) -> None:
        if self._flush_exc is not None:
            raise self._flush_exc
        self.flush_count += 1


class RaceLock:
    """Simulates a race that flips `_disabled` True while the lock is held,
    so the inner double-checked guard is exercised deterministically."""

    def __init__(self, target: _ResilientTextStream) -> None:
        self.target = target

    def __enter__(self) -> None:
        self.target._disabled = True

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def restore_streams():
    out, err = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = out, err


class TestWrite:
    def test_healthy_write_appends_and_returns_len(self) -> None:
        stream = FakeStream()
        resilient = _ResilientTextStream(stream)
        assert resilient.write("hi") == 2
        assert stream.written == ["hi"]

    def test_disabled_write_skips_stream(self) -> None:
        stream = FakeStream()
        resilient = _ResilientTextStream(stream)
        resilient._disabled = True
        assert resilient.write("hi") == 2
        assert stream.written == []

    def test_broken_pipe_disables_then_skips(self) -> None:
        stream = FakeStream(write_exc=BrokenPipeError())
        resilient = _ResilientTextStream(stream)
        assert resilient.write("hi") == 2
        assert resilient._disabled is True
        assert resilient.write("again") == 5
        assert stream.written == []

    def test_connection_reset_disables(self) -> None:
        stream = FakeStream(write_exc=ConnectionResetError())
        resilient = _ResilientTextStream(stream)
        assert resilient.write("x") == 1
        assert resilient._disabled is True

    def test_inner_disabled_guard_via_race_lock(self) -> None:
        stream = FakeStream()
        resilient = _ResilientTextStream(stream)
        resilient._lock = RaceLock(resilient)
        assert resilient.write("hi") == 2
        assert stream.written == []


class TestFlush:
    def test_healthy_flush_calls_stream(self) -> None:
        stream = FakeStream()
        resilient = _ResilientTextStream(stream)
        resilient.flush()
        assert stream.flush_count == 1

    def test_disabled_flush_noop(self) -> None:
        stream = FakeStream()
        resilient = _ResilientTextStream(stream)
        resilient._disabled = True
        resilient.flush()
        assert stream.flush_count == 0

    def test_broken_pipe_on_flush_disables(self) -> None:
        stream = FakeStream(flush_exc=BrokenPipeError())
        resilient = _ResilientTextStream(stream)
        resilient.flush()
        assert resilient._disabled is True
        resilient.flush()
        assert stream.flush_count == 0

    def test_inner_disabled_guard_via_race_lock(self) -> None:
        stream = FakeStream()
        resilient = _ResilientTextStream(stream)
        resilient._lock = RaceLock(resilient)
        resilient.flush()
        assert stream.flush_count == 0


class TestGetattr:
    def test_delegates_known_attr(self) -> None:
        resilient = _ResilientTextStream(FakeStream())
        assert resilient.name == "fake"

    def test_missing_attr_raises(self) -> None:
        resilient = _ResilientTextStream(FakeStream())
        with pytest.raises(AttributeError):
            resilient.does_not_exist  # noqa: B018


class TestProtectStandardStreams:
    def test_wraps_both_streams(self, restore_streams) -> None:
        sys.stdout = FakeStream()
        sys.stderr = FakeStream()
        protect_standard_streams()
        assert isinstance(sys.stdout, _ResilientTextStream)
        assert isinstance(sys.stderr, _ResilientTextStream)

    def test_idempotent_no_rewrap(self, restore_streams) -> None:
        sys.stdout = FakeStream()
        sys.stderr = FakeStream()
        protect_standard_streams()
        wrapped_out = sys.stdout
        wrapped_err = sys.stderr
        protect_standard_streams()
        assert sys.stdout is wrapped_out
        assert sys.stderr is wrapped_err

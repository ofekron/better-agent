from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json_store  # noqa: E402

import pytest  # noqa: E402


def test_write_json_uses_unique_temp_file_per_write() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        sources: list[str] = []
        real_replace = json_store.os.replace

        def recording_replace(src, dst):
            sources.append(Path(src).name)
            real_replace(src, dst)

        json_store.os.replace = recording_replace
        try:
            json_store.write_json(path, {"n": 1})
            json_store.write_json(path, {"n": 2})
        finally:
            json_store.os.replace = real_replace

        assert len(sources) == 2
        assert len(set(sources)) == 2, sources
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 2}
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_write_json_retries_transient_windows_permission_error() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        real_replace = json_store.os.replace
        real_sleep = json_store.time.sleep
        real_is_windows = json_store._is_windows
        attempts = 0
        sleeps: list[float] = []

        def flaky_replace(src, dst):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError(5, "Access is denied")
            real_replace(src, dst)

        json_store.os.replace = flaky_replace
        json_store.time.sleep = sleeps.append
        json_store._is_windows = lambda: True
        try:
            json_store.write_json(path, {"n": 1})
        finally:
            json_store.os.replace = real_replace
            json_store.time.sleep = real_sleep
            json_store._is_windows = real_is_windows

        assert attempts == 3
        assert len(sleeps) == 2
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_write_json_durable_tolerates_unsupported_windows_directory_fsync() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        real_open = json_store.os.open
        real_is_windows = json_store._is_windows
        denied_directory_open = False

        def windows_open(candidate, flags, *args, **kwargs):
            nonlocal denied_directory_open
            if Path(candidate) == path.parent:
                denied_directory_open = True
                raise PermissionError(13, "Permission denied")
            return real_open(candidate, flags, *args, **kwargs)

        json_store.os.open = windows_open
        json_store._is_windows = lambda: True
        try:
            json_store.write_json_durable(path, {"n": 1})
        finally:
            json_store.os.open = real_open
            json_store._is_windows = real_is_windows

        assert denied_directory_open
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


# --- read_json ---

def test_read_json_missing_file_returns_default() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        assert json_store.read_json(Path(tmpdir) / "absent.json", {"d": 1}) == {"d": 1}


def test_read_json_malformed_returns_default() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        path.write_text("{not json", encoding="utf-8")
        assert json_store.read_json(path, [1, 2]) == [1, 2]


def test_read_json_wrong_type_returns_default() -> None:
    # A hand-edited file that flipped a list into a dict must not poison the store.
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        path.write_text(json.dumps({"k": "v"}), encoding="utf-8")
        assert json_store.read_json(path, ["expected-list"]) == ["expected-list"]


def test_read_json_matching_type_returns_data() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        path.write_text(json.dumps({"k": "v"}), encoding="utf-8")
        assert json_store.read_json(path, {}) == {"k": "v"}


# --- _replace_atomic non-windows + final-retry branches ---

def test_replace_atomic_non_windows_reraises_permission_error_immediately() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        src = Path(tmpdir) / "src"
        dst = Path(tmpdir) / "dst"
        src.write_text("x", encoding="utf-8")
        real_replace = json_store.os.replace
        real_sleep = json_store.time.sleep

        def denying_replace(s, d):
            raise PermissionError(5, "denied")

        json_store.os.replace = denying_replace
        json_store.time.sleep = lambda _d: None  # would be a bug if reached
        try:
            with pytest.raises(PermissionError):
                json_store._replace_atomic(src, dst)
        finally:
            json_store.os.replace = real_replace
            json_store.time.sleep = real_sleep


def test_replace_atomic_windows_falls_through_to_final_replace() -> None:
    # Every retry slot raises; the final unconditional os.replace must still run.
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        src = Path(tmpdir) / "src"
        dst = Path(tmpdir) / "dst"
        src.write_text("payload", encoding="utf-8")
        real_replace = json_store.os.replace
        real_sleep = json_store.time.sleep
        real_is_windows = json_store._is_windows
        attempts = 0

        def always_denied(s, d):
            nonlocal attempts
            attempts += 1
            # The loop body raises for each delay; the post-loop call succeeds.
            if attempts <= len(json_store._WINDOWS_REPLACE_RETRY_DELAYS_S):
                raise PermissionError(5, "denied")
            real_replace(s, d)

        json_store.os.replace = always_denied
        json_store.time.sleep = lambda _d: None
        json_store._is_windows = lambda: True
        try:
            json_store._replace_atomic(src, dst)
        finally:
            json_store.os.replace = real_replace
            json_store.time.sleep = real_sleep
            json_store._is_windows = real_is_windows

        assert attempts == len(json_store._WINDOWS_REPLACE_RETRY_DELAYS_S) + 1
        assert dst.read_text(encoding="utf-8") == "payload"


# --- _fsync_parent_directory ---

def test_fsync_parent_directory_succeeds_on_posix() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        target = Path(tmpdir) / "nested" / "file.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")
        # No exception means the success path ran end-to-end.
        json_store._fsync_parent_directory(target)


def test_fsync_parent_directory_non_windows_reraises_oserror() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        target = Path(tmpdir) / "file.json"
        real_open = json_store.os.open
        real_is_windows = json_store._is_windows

        def failing_open(_p, _flags, *a, **k):
            raise OSError("simulated")

        json_store.os.open = failing_open
        json_store._is_windows = lambda: False
        try:
            with pytest.raises(OSError):
                json_store._fsync_parent_directory(target)
        finally:
            json_store.os.open = real_open
            json_store._is_windows = real_is_windows


# --- write_json durable success fsync + write_json/write_private_text failure cleanup ---

def test_write_json_durable_runs_real_fsync_success_path() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        json_store.write_json_durable(path, {"n": 1})
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_write_json_cleans_up_tmp_on_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        real_dump = json_store.json.dump

        def failing_dump(_data, _handle, **_kw):
            raise RuntimeError("boom")

        json_store.json.dump = failing_dump
        try:
            with pytest.raises(RuntimeError):
                json_store.write_json(path, {"n": 1})
        finally:
            json_store.json.dump = real_dump

        # Failed before replace: target never created, no tmp debris.
        assert not path.exists()
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_write_json_swallows_unlink_oserror_during_cleanup() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        real_dump = json_store.json.dump
        real_path_unlink = json_store.Path.unlink

        def failing_dump(_data, _handle, **_kw):
            raise RuntimeError("boom")

        def unlink_always_fails(self, *a, **k):  # noqa: ANN001
            raise OSError("cannot unlink")

        json_store.json.dump = failing_dump
        json_store.Path.unlink = unlink_always_fails
        try:

            # The OSError from unlink is swallowed; the original RuntimeError propagates.
            with pytest.raises(RuntimeError):
                json_store.write_json(path, {"n": 1})
        finally:
            json_store.json.dump = real_dump
            json_store.Path.unlink = real_path_unlink


def test_write_json_durable_cleans_up_tmp_on_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        real_dump = json_store.json.dump

        def failing_dump(_data, _handle, **_kw):
            raise RuntimeError("boom")

        json_store.json.dump = failing_dump
        try:
            with pytest.raises(RuntimeError):
                json_store.write_json_durable(path, {"n": 1})
        finally:
            json_store.json.dump = real_dump

        assert not path.exists()
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


# --- write_private_text ---

def test_write_private_text_writes_restricted_file_atomically() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "secret.txt"
        json_store.write_private_text(path, "top-secret-payload")
        assert path.read_text(encoding="utf-8") == "top-secret-payload"
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))
        # On POSIX the file is chmod'd private by make_private_file.
        if json_store.os.name != "nt":
            assert (path.stat().st_mode & 0o077) == 0


def test_write_private_text_cleans_up_tmp_on_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "secret.txt"
        real_write = json_store.os.fdopen

        def failing_fdopen(_fd, *a, **k):
            raise RuntimeError("boom")

        json_store.os.fdopen = failing_fdopen
        try:
            with pytest.raises(RuntimeError):
                json_store.write_private_text(path, "x")
        finally:
            json_store.os.fdopen = real_write

        assert not path.exists()
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def main() -> None:
    test_write_json_uses_unique_temp_file_per_write()
    test_write_json_retries_transient_windows_permission_error()
    test_write_json_durable_tolerates_unsupported_windows_directory_fsync()
    test_read_json_missing_file_returns_default()
    test_read_json_malformed_returns_default()
    test_read_json_wrong_type_returns_default()
    test_read_json_matching_type_returns_data()
    test_replace_atomic_non_windows_reraises_permission_error_immediately()
    test_replace_atomic_windows_falls_through_to_final_replace()
    test_fsync_parent_directory_succeeds_on_posix()
    test_write_json_durable_runs_real_fsync_success_path()
    test_write_json_cleans_up_tmp_on_failure()
    test_write_private_text_writes_restricted_file_atomically()
    print("PASS json_store atomic and durable writes are portable across supported platforms")


if __name__ == "__main__":
    main()

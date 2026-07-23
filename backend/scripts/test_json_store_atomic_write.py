from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json_store  # noqa: E402


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


def main() -> None:
    test_write_json_uses_unique_temp_file_per_write()
    test_write_json_retries_transient_windows_permission_error()
    test_write_json_durable_tolerates_unsupported_windows_directory_fsync()
    print("PASS json_store atomic and durable writes are portable across supported platforms")


if __name__ == "__main__":
    main()

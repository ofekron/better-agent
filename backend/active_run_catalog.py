from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

VERSION = 1
_LOCK = threading.RLock()
_LOCAL = threading.local()


def _path(root: Path) -> Path:
    return root / "active_run_catalog.json"


def _dirty_path(root: Path) -> Path:
    return root / "active_run_catalog.dirty"


def _transaction_path(root: Path, token: str) -> Path:
    return root / f"active_run_catalog.dirty.{token}"


def _dirty_tokens(root: Path) -> list[Path]:
    return list(root.glob("active_run_catalog.dirty.*"))


def _local_tokens() -> dict[str, list[str]]:
    tokens = getattr(_LOCAL, "tokens", None)
    if tokens is None:
        tokens = {}
        _LOCAL.tokens = tokens
    return tokens


def _take_local_token(root: Path) -> str | None:
    owned = _local_tokens().get(str(root), [])
    token = owned.pop() if owned else None
    if not owned:
        _local_tokens().pop(str(root), None)
    return token


def _forget_local_token(root: Path, token: str) -> None:
    owned = _local_tokens().get(str(root), [])
    try:
        owned.remove(token)
    except ValueError:
        return
    if not owned:
        _local_tokens().pop(str(root), None)


def _valid(run_id: str) -> bool:
    return bool(run_id and run_id not in {".", ".."} and os.sep not in run_id and (os.altsep is None or os.altsep not in run_id))


def _is_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(int(getattr(st, "st_file_attributes", 0) or 0) & 0x400)


def _require_safe_root(root: Path) -> None:
    st = root.lstat()
    if (
        not stat.S_ISDIR(st.st_mode)
        or root.is_symlink()
        or bool(int(getattr(st, "st_file_attributes", 0) or 0) & 0x400)
    ):
        raise OSError("runs root is not a safe directory")


def _valid_component(value: str) -> bool:
    return bool(value and value not in {".", ".."} and "/" not in value and "\\" not in value and "\0" not in value)


def read_relative(root: Path, *components: str) -> bytes:
    if not components or any(not _valid_component(component) for component in components):
        raise ValueError("invalid relative catalog path")
    _require_safe_root(root)
    if os.name == "nt":
        from windows_handle_marker import WindowsNativeOps
        return WindowsNativeOps().read_file_relative(root, tuple(components))
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    handles: list[int] = []
    try:
        current = os.open(root, directory_flags)
        handles.append(current)
        root_identity = os.fstat(current)
        for component in components[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            handles.append(current)
        file_fd = os.open(components[-1], file_flags, dir_fd=current)
        handles.append(file_fd)
        before = os.fstat(file_fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        current_root = root.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
            or (root_identity.st_dev, root_identity.st_ino)
            != (current_root.st_dev, current_root.st_ino)
        ):
            raise OSError("relative catalog file changed during read")
        return b"".join(chunks)
    finally:
        for handle in reversed(handles):
            os.close(handle)


def load(root: Path, *, allow_dirty: bool = False) -> dict[str, dict] | None:
    path = _path(root)
    try:
        _require_safe_root(root)
        if not allow_dirty and (_dirty_path(root).exists() or _dirty_tokens(root)):
            return None
        if _is_reparse(path):
            return None
        raw = json.loads(read_relative(root, path.name).decode("utf-8"))
        if raw.get("version") != VERSION or not isinstance(raw.get("runs"), dict):
            return None
        result = {}
        for run_id, record in raw["runs"].items():
            if not _valid(run_id) or not isinstance(record, dict):
                return None
            provider_id = record.get("provider_id")
            if provider_id is not None and not isinstance(provider_id, str):
                return None
            result[run_id] = {"provider_id": provider_id}
        return result
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write(root: Path, runs: dict[str, dict]) -> None:
    _require_safe_root(root)
    path = _path(root)
    temp = root / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps({"version": VERSION, "runs": runs}, separators=(",", ":")).encode()
    if os.name == "nt":
        from windows_handle_marker import WindowsNativeOps, write_atomic_file
        write_atomic_file(WindowsNativeOps(), root, path.name, payload)
        return
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        dir_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _encode_intent(intent: dict | None) -> bytes:
    if intent is None:
        return b""
    operation = intent.get("operation")
    runs = intent.get("runs")
    if operation not in {"register", "retire"} or not isinstance(runs, list) or not runs:
        raise ValueError("invalid catalog mutation intent")
    normalized: list[dict] = []
    seen: set[str] = set()
    for record in runs:
        if not isinstance(record, dict) or not _valid(str(record.get("run_id") or "")):
            raise ValueError("invalid catalog mutation run")
        run_id = str(record["run_id"])
        if run_id in seen:
            continue
        seen.add(run_id)
        provider_id = record.get("provider_id")
        if provider_id is not None and not isinstance(provider_id, str):
            raise ValueError("invalid catalog mutation provider")
        normalized.append({"run_id": run_id, "provider_id": provider_id})
    return json.dumps(
        {"version": 1, "operation": operation, "runs": normalized},
        separators=(",", ":"),
    ).encode("utf-8")


def _mark_dirty_locked(
    root: Path,
    *,
    remember_local: bool = False,
    intent: dict | None = None,
) -> str:
    _require_safe_root(root)
    token = uuid.uuid4().hex
    path = _transaction_path(root, token)
    payload = _encode_intent(intent)
    if os.name == "nt":
        from windows_handle_marker import WindowsNativeOps, write_atomic_file
        write_atomic_file(WindowsNativeOps(), root, path.name, payload)
        if not _dirty_path(root).exists():
            write_atomic_file(WindowsNativeOps(), root, _dirty_path(root).name, b"")
        if remember_local:
            _local_tokens().setdefault(str(root), []).append(token)
        return token
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if payload:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short catalog intent write")
                view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    aggregate = _dirty_path(root)
    if not aggregate.exists():
        aggregate_fd = os.open(
            aggregate,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fsync(aggregate_fd)
        finally:
            os.close(aggregate_fd)
    _fsync_dir(root)
    if remember_local:
        _local_tokens().setdefault(str(root), []).append(token)
    return token


def _clear_dirty_locked(root: Path, token: str | None = None) -> None:
    if token is None:
        token = _take_local_token(root)
    else:
        _forget_local_token(root, token)
    path = _transaction_path(root, token) if token else _dirty_path(root)
    if os.name == "nt" and path.exists():
        from windows_handle_marker import WindowsNativeOps
        ops = WindowsNativeOps()
        root_handle = ops.open_root(root)
        try:
            if ops.stat(root_handle).reparse:
                raise OSError("runs root is a reparse point")
            ops.delete_relative(root_handle, path.name)
            if token and not _dirty_tokens(root):
                ops.delete_relative(root_handle, _dirty_path(root).name)
            ops.flush(root_handle)
        finally:
            ops.close(root_handle)
        return
    path.unlink(missing_ok=True)
    if token and not _dirty_tokens(root):
        _dirty_path(root).unlink(missing_ok=True)
    _fsync_dir(root)


class CatalogTransaction:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._stack = ExitStack()
        self._entered = False
        self._root_identity: tuple[int, int] | None = None

    def __enter__(self) -> "CatalogTransaction":
        if self._entered:
            raise RuntimeError("catalog transaction is non-reentrant")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _require_safe_root(self.root)
            self._stack.enter_context(_LOCK)
            from runs_dir import run_catalog_lock
            self._stack.enter_context(run_catalog_lock(self.root))
            _require_safe_root(self.root)
            st = self.root.lstat()
            self._root_identity = (st.st_dev, st.st_ino)
            self._entered = True
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *exc: object) -> None:
        self._entered = False
        self._root_identity = None
        self._stack.close()

    def mark_dirty(self, intent: dict | None = None) -> str:
        self._check()
        return _mark_dirty_locked(self.root, intent=intent)

    def clear_dirty(self, token: str | None = None) -> None:
        self._check()
        _clear_dirty_locked(self.root, token)

    def register(self, path: Path, state: dict, token: str) -> None:
        self._check()
        _register_locked(path, state, token, self._check)

    def retire_many(self, run_ids: list[str], token: str) -> None:
        self._check()
        _retire_many_locked(self.root, run_ids, token, self._check)

    def load_or_rebuild(self) -> tuple[dict[str, dict], bool]:
        self._check()
        runs = load(self.root)
        self._check()
        if runs is not None:
            return runs, False
        repaired = _repair_typed_intents(self.root, self._check)
        if repaired is not None:
            return repaired, True
        return _rebuild(self.root, self._check), True

    def _check(self) -> None:
        if not self._entered:
            raise RuntimeError("catalog transaction is not active")
        _require_safe_root(self.root)
        st = self.root.lstat()
        if (st.st_dev, st.st_ino) != self._root_identity:
            raise OSError("runs root changed during catalog transaction")


def transaction(root: Path) -> CatalogTransaction:
    return CatalogTransaction(root)


def mark_dirty(root: Path, intent: dict | None = None) -> str:
    with transaction(root) as current:
        token = _mark_dirty_locked(root, remember_local=True, intent=intent)
        return token


def clear_dirty(root: Path, token: str | None = None) -> None:
    with transaction(root) as current:
        current.clear_dirty(token)


def _fsync_dir(root: Path) -> None:
    try:
        fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        if os.name != "nt":
            raise


def _scan(root: Path) -> dict[str, dict]:
    _require_safe_root(root)
    runs = {}
    if root.exists():
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False) or not _valid(entry.name):
                    continue
                run_dir = Path(entry.path)
                marker_path = run_dir / "reconciled.marker"
                if marker_path.exists() and not marker_path.is_symlink():
                    try:
                        from ingestion_versions import marker_data_matches_current
                        marker = json.loads(read_relative(root, entry.name, marker_path.name).decode("utf-8"))
                        if marker_data_matches_current(
                            marker, str(marker.get("provider_kind") or ""),
                        ):
                            continue
                    except (OSError, json.JSONDecodeError, TypeError):
                        pass
                provider_id = None
                try:
                    state = json.loads(read_relative(root, entry.name, "backend_state.json").decode("utf-8"))
                    if isinstance(state, dict) and isinstance(state.get("provider_id"), str):
                        provider_id = state["provider_id"]
                except (OSError, json.JSONDecodeError):
                    pass
                runs[entry.name] = {"provider_id": provider_id}
    return runs


def _read_intent(root: Path, token_path: Path) -> dict | None:
    try:
        raw = json.loads(read_relative(root, token_path.name).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    operation = raw.get("operation")
    records = raw.get("runs")
    if operation not in {"register", "retire"} or not isinstance(records, list) or not records:
        return None
    normalized: list[dict] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            return None
        run_id = record.get("run_id")
        provider_id = record.get("provider_id")
        if not isinstance(run_id, str) or not _valid(run_id) or run_id in seen:
            return None
        if provider_id is not None and not isinstance(provider_id, str):
            return None
        seen.add(run_id)
        normalized.append({"run_id": run_id, "provider_id": provider_id})
    return {"operation": operation, "runs": normalized}


def _current_terminal_marker(root: Path, run_id: str) -> bool:
    try:
        marker = json.loads(
            read_relative(root, run_id, "reconciled.marker").decode("utf-8")
        )
        from ingestion_versions import marker_data_matches_current
        return isinstance(marker, dict) and marker_data_matches_current(
            marker, str(marker.get("provider_kind") or ""),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False


def _repair_typed_intents(
    root: Path,
    validate: Callable[[], None] | None = None,
) -> dict[str, dict] | None:
    import time
    import perf

    started = time.perf_counter()
    tokens = _dirty_tokens(root)
    perf.record_count("startup.recovery.catalog_repair.tokens", len(tokens))
    if not tokens:
        return None
    runs = load(root, allow_dirty=True)
    if runs is None:
        perf.record_count("startup.recovery.catalog_repair.fallback", 1)
        return None
    intents: list[dict] = []
    for token_path in tokens:
        intent = _read_intent(root, token_path)
        if intent is None:
            perf.record_count("startup.recovery.catalog_repair.fallback", 1)
            return None
        intents.append(intent)
    if validate is not None:
        validate()
    authority_started = time.perf_counter()
    changed = False
    repaired_runs = 0
    touched = {
        record["run_id"]
        for intent in intents
        for record in intent["runs"]
    }
    for run_id in touched:
        if _current_terminal_marker(root, run_id):
            changed = runs.pop(run_id, None) is not None or changed
            repaired_runs += 1
            continue
        try:
            state = json.loads(
                read_relative(root, run_id, "backend_state.json").decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or state.get("run_id") != run_id:
            continue
        provider_id = state.get("provider_id")
        if provider_id is not None and not isinstance(provider_id, str):
            continue
        value = {"provider_id": provider_id}
        if runs.get(run_id) != value:
            runs[run_id] = value
            changed = True
        repaired_runs += 1
    perf.record(
        "startup.recovery.catalog_repair.authority_reads",
        (time.perf_counter() - authority_started) * 1000.0,
    )
    if validate is not None:
        validate()
    persist_started = time.perf_counter()
    if changed:
        _write(root, runs)
    for token_path in tokens:
        token_path.unlink(missing_ok=True)
    _dirty_path(root).unlink(missing_ok=True)
    _fsync_dir(root)
    perf.record(
        "startup.recovery.catalog_repair.persist",
        (time.perf_counter() - persist_started) * 1000.0,
    )
    perf.record_count("startup.recovery.catalog_repair.runs", repaired_runs)
    perf.record(
        "startup.recovery.catalog_repair.total",
        (time.perf_counter() - started) * 1000.0,
    )
    return runs


def _rebuild(root: Path, validate: Callable[[], None] | None = None) -> dict[str, dict]:
    import time
    import perf

    started = time.perf_counter()
    _require_safe_root(root)
    if validate is not None:
        validate()
    scan_started = time.perf_counter()
    runs = _scan(root)
    perf.record(
        "startup.recovery.catalog_rebuild.scan",
        (time.perf_counter() - scan_started) * 1000.0,
    )
    if validate is not None:
        validate()
    _write(root, runs)
    if validate is not None:
        validate()
    _dirty_path(root).unlink(missing_ok=True)
    for token_path in _dirty_tokens(root):
        token_path.unlink(missing_ok=True)
    _fsync_dir(root)
    perf.record_count("startup.recovery.catalog_rebuild.runs", len(runs))
    perf.record(
        "startup.recovery.catalog_rebuild.total",
        (time.perf_counter() - started) * 1000.0,
    )
    return runs


def load_or_rebuild(root: Path) -> tuple[dict[str, dict], bool]:
    with transaction(root) as current:
        return current.load_or_rebuild()


def register(path: Path, state: dict, *, dirty_token: str | None = None) -> None:
    root = path.parent.parent
    owned_token = dirty_token or _take_local_token(root)
    with transaction(root) as current:
        token = owned_token or current.mark_dirty({
            "operation": "register",
            "runs": [{
                "run_id": state.get("run_id"),
                "provider_id": state.get("provider_id"),
            }],
        })
        current.register(path, state, token)


def _register_locked(
    path: Path,
    state: dict,
    dirty_token: str,
    validate: Callable[[], None] | None = None,
) -> None:
    run_id = state.get("run_id")
    if path.name != "backend_state.json" or not isinstance(run_id, str) or not _valid(run_id) or path.parent.name != run_id or _is_reparse(path.parent):
        raise ValueError("invalid active run registration")
    root = path.parent.parent
    _require_safe_root(root)
    runs = load(root, allow_dirty=True)
    if validate is not None:
        validate()
    needs_write = runs is None
    if runs is None:
        runs = _scan(root)
        if validate is not None:
            validate()
    record = {"provider_id": state.get("provider_id")}
    if runs.get(run_id) != record:
        runs[run_id] = record
        needs_write = True
    if needs_write:
        _write(root, runs)
        if validate is not None:
            validate()
    if validate is not None:
        validate()
    _clear_dirty_locked(root, dirty_token)


def retire(root: Path, run_id: str, *, dirty_token: str | None = None) -> None:
    retire_many(root, [run_id], dirty_token=dirty_token)


def retire_many(root: Path, run_ids: list[str], *, dirty_token: str | None = None) -> None:
    if not run_ids:
        return
    owned_token = dirty_token or _take_local_token(root)
    with transaction(root) as current:
        token = owned_token or current.mark_dirty({
            "operation": "retire",
            "runs": [{"run_id": run_id, "provider_id": None} for run_id in run_ids],
        })
        current.retire_many(run_ids, token)


def _retire_many_locked(
    root: Path,
    run_ids: list[str],
    dirty_token: str,
    validate: Callable[[], None] | None = None,
) -> None:
    if any(not _valid(run_id) for run_id in run_ids):
        raise ValueError("invalid run id")
    if not run_ids:
        _clear_dirty_locked(root, dirty_token)
        return
    runs = load(root, allow_dirty=True)
    if validate is not None:
        validate()
    if runs is None:
        _clear_dirty_locked(root, dirty_token)
        return
    changed = False
    for run_id in run_ids:
        changed = runs.pop(run_id, None) is not None or changed
    if changed:
        _write(root, runs)
        if validate is not None:
            validate()
    if validate is not None:
        validate()
    _clear_dirty_locked(root, dirty_token)

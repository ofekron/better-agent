from __future__ import annotations

import ctypes
import hashlib
import msvcrt
import os
import time
from ctypes import wintypes
from pathlib import Path, PurePosixPath
from typing import Any


_EMPTY = {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_GENERIC_READ = 0x80000000
_SYNCHRONIZE = 0x00100000
_SHARE_ALL = 7
_OPEN_EXISTING = 3
_OPEN_REPARSE = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_REPARSE_ATTRIBUTE = 0x00000400
_DIRECTORY_ATTRIBUTE = 0x00000010
_OBJ_CASE_INSENSITIVE = 0x40
_FILE_OPEN = 1
_FILE_DIRECTORY_FILE = 1
_FILE_NON_DIRECTORY_FILE = 0x40
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_ID_BOTH_DIRECTORY_INFO = 10
_FILE_BASIC_INFO = 0
_ERROR_NO_MORE_FILES = 18

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")
_CloseHandle = _kernel32.CloseHandle


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR)]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)), ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID), ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD), ("creation_low", wintypes.DWORD),
        ("creation_high", wintypes.DWORD), ("access_low", wintypes.DWORD),
        ("access_high", wintypes.DWORD), ("write_low", wintypes.DWORD),
        ("write_high", wintypes.DWORD), ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD), ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong), ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD), ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD), ("ShortNameLength", ctypes.c_ubyte),
        ("ShortName", wintypes.WCHAR * 12), ("FileId", ctypes.c_longlong),
        ("FileName", wintypes.WCHAR * 1),
    ]


class _FILE_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    ]


def fingerprint_package_windows(spec: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    root_path = Path(str(spec["root"])).expanduser()
    root = _open_absolute_root(root_path)
    if root is None:
        return dict(_EMPTY)
    try:
        root_identity = _handle_identity(root)
        declared = _declared_paths(spec, root)
        if root_identity is None or declared is None:
            return dict(_EMPTY)
        files: set[str] = set()
        directories: dict[str, tuple[tuple[int, ...], str, int]] = {}
        for relative in declared:
            handle = _open_relative(root, relative, directory=None)
            if handle is None:
                return dict(_EMPTY)
            try:
                info = _file_info(handle)
                if info is None or info.attributes & _REPARSE_ATTRIBUTE:
                    return dict(_EMPTY)
                if info.attributes & _DIRECTORY_ATTRIBUTE:
                    if not _collect_directory(handle, relative, files, directories):
                        return dict(_EMPTY)
                else:
                    files.add(relative)
            finally:
                _CloseHandle(handle)
        scan_ms = (time.perf_counter() - started) * 1000.0
        result = _hash_files(root, sorted(files), scan_ms)
        if result["digest"] is None or _handle_identity(root) != root_identity:
            return dict(_EMPTY)
        for relative, (identity, membership_digest, membership_count) in directories.items():
            handle = _open_relative(root, relative, directory=True)
            if handle is None:
                return dict(_EMPTY)
            try:
                entries = _directory_entries(handle)
                if (
                    entries is None
                    or _handle_identity(handle) != identity
                    or _entries_digest(entries) != membership_digest
                    or len(entries) != membership_count
                ):
                    return dict(_EMPTY)
            finally:
                _CloseHandle(handle)
        verify_root = _open_absolute_root(root_path)
        if verify_root is None:
            return dict(_EMPTY)
        try:
            if _handle_identity(verify_root) != root_identity:
                return dict(_EMPTY)
        finally:
            _CloseHandle(verify_root)
        return result
    finally:
        _CloseHandle(root)


def _declared_paths(spec: dict[str, Any], root: int) -> set[str] | None:
    paths: set[str] = set()
    for value in spec.get("relative_paths") or ():
        clean = _clean_relative(str(value))
        if clean is None:
            return None
        paths.add(clean)
    static = dict(spec.get("static_modules") or {})
    for module in spec.get("modules") or ():
        if module in static:
            clean = _clean_relative(str(static[module]))
            if clean is None:
                return None
            paths.add(clean)
            continue
        module_path = PurePosixPath(*str(module).split("."))
        candidates = (str(module_path.with_suffix(".py")), str(module_path / "__init__.py"))
        match = None
        for candidate in candidates:
            handle = _open_relative(root, candidate, directory=False)
            if handle is not None:
                _CloseHandle(handle)
                match = candidate
                break
        if match is None:
            return None
        paths.add(match)
    return paths


def _clean_relative(value: str) -> str | None:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _open_absolute_root(path: Path) -> int | None:
    create = _kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    handle = create(
        str(path), _GENERIC_READ | _SYNCHRONIZE, _SHARE_ALL, None, _OPEN_EXISTING,
        _OPEN_REPARSE | _BACKUP_SEMANTICS, None,
    )
    if handle == _INVALID_HANDLE:
        return None
    info = _file_info(handle)
    if info is None or info.attributes & _REPARSE_ATTRIBUTE or not info.attributes & _DIRECTORY_ATTRIBUTE:
        _CloseHandle(handle)
        return None
    return handle


def _open_relative(root: int, relative: str, *, directory: bool | None) -> int | None:
    current = root
    owned: int | None = None
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1
        want_directory = True if not is_last else directory
        next_handle = _nt_open_component(current, part, directory=want_directory)
        if owned is not None:
            _CloseHandle(owned)
        if next_handle is None:
            return None
        owned = next_handle
        current = next_handle
    return owned


def _nt_open_component(parent: int, name: str, *, directory: bool | None) -> int | None:
    buffer = ctypes.create_unicode_buffer(name)
    unicode = _UNICODE_STRING(len(name) * 2, (len(name) + 1) * 2, ctypes.cast(buffer, wintypes.LPWSTR))
    attributes = _OBJECT_ATTRIBUTES(
        ctypes.sizeof(_OBJECT_ATTRIBUTES), parent, ctypes.pointer(unicode),
        _OBJ_CASE_INSENSITIVE, None, None,
    )
    iosb = _IO_STATUS_BLOCK()
    handle = wintypes.HANDLE()
    options = _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
    if directory is True:
        options |= _FILE_DIRECTORY_FILE
    elif directory is False:
        options |= _FILE_NON_DIRECTORY_FILE
    status = _ntdll.NtCreateFile(
        ctypes.byref(handle), _GENERIC_READ | _SYNCHRONIZE, ctypes.byref(attributes),
        ctypes.byref(iosb), None, 0, _SHARE_ALL, _FILE_OPEN, options, None, 0,
    )
    if ctypes.c_long(status).value < 0:
        return None
    info = _file_info(handle.value)
    if info is None or info.attributes & _REPARSE_ATTRIBUTE:
        _CloseHandle(handle.value)
        return None
    return handle.value


def _collect_directory(
    directory: int,
    prefix: str,
    files: set[str],
    directories: dict[str, tuple[tuple[int, ...], str, int]],
) -> bool:
    entries = _directory_entries(directory)
    if entries is None:
        return False
    identity = _handle_identity(directory)
    if identity is None:
        return False
    directories[prefix] = (identity, _entries_digest(entries), len(entries))
    for name, attributes in entries:
        if name in {".", ".."} or attributes & _REPARSE_ATTRIBUTE:
            if attributes & _REPARSE_ATTRIBUTE:
                return False
            continue
        relative = f"{prefix}/{name}"
        is_directory = bool(attributes & _DIRECTORY_ATTRIBUTE)
        handle = _nt_open_component(directory, name, directory=is_directory)
        if handle is None:
            return False
        try:
            if is_directory:
                if not _collect_directory(handle, relative, files, directories):
                    return False
            else:
                files.add(relative)
        finally:
            _CloseHandle(handle)
    return True


def _directory_entries(handle: int) -> list[tuple[str, int]] | None:
    entries: list[tuple[str, int]] = []
    while True:
        buffer = ctypes.create_string_buffer(64 * 1024)
        ok = _kernel32.GetFileInformationByHandleEx(
            handle, _FILE_ID_BOTH_DIRECTORY_INFO, buffer, len(buffer),
        )
        if not ok:
            if ctypes.get_last_error() == _ERROR_NO_MORE_FILES:
                return entries
            return None
        offset = 0
        while True:
            info = _FILE_ID_BOTH_DIR_INFO.from_buffer(buffer, offset)
            name_address = ctypes.addressof(buffer) + offset + _FILE_ID_BOTH_DIR_INFO.FileName.offset
            name = ctypes.wstring_at(name_address, info.FileNameLength // 2)
            entries.append((name, info.FileAttributes))
            if info.NextEntryOffset == 0:
                break
            offset += info.NextEntryOffset


def _entries_digest(entries: list[tuple[str, int]]) -> str:
    digest = hashlib.sha256()
    for name, attributes in sorted(entries):
        digest.update(name.encode("utf-16-le"))
        digest.update(b"\0")
        digest.update(int(attributes).to_bytes(4, "little"))
    return digest.hexdigest()


def _hash_files(root: int, files: list[str], scan_ms: float) -> dict[str, Any]:
    digest = hashlib.sha256()
    bytes_read = 0
    started = time.perf_counter()
    for relative in files:
        handle = _open_relative(root, relative, directory=False)
        if handle is None:
            return dict(_EMPTY)
        identity = _handle_identity(handle)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        try:
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            while chunk := os.read(fd, 1024 * 1024):
                bytes_read += len(chunk)
                digest.update(chunk)
            digest.update(b"\0")
            if _handle_identity(msvcrt.get_osfhandle(fd)) != identity:
                return dict(_EMPTY)
        finally:
            os.close(fd)
        verify = _open_relative(root, relative, directory=False)
        if verify is None:
            return dict(_EMPTY)
        try:
            if _handle_identity(verify) != identity:
                return dict(_EMPTY)
        finally:
            _CloseHandle(verify)
    return {
        "digest": digest.hexdigest(), "files": len(files), "bytes": bytes_read,
        "scan_ms": scan_ms, "hash_ms": (time.perf_counter() - started) * 1000.0,
    }


def _file_info(handle: int) -> _BY_HANDLE_FILE_INFORMATION | None:
    info = _BY_HANDLE_FILE_INFORMATION()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        return None
    return info


def _handle_identity(handle: int) -> tuple[int, ...] | None:
    info = _file_info(handle)
    basic = _FILE_BASIC_INFORMATION()
    if (
        info is None
        or not _kernel32.GetFileInformationByHandleEx(
            handle, _FILE_BASIC_INFO, ctypes.byref(basic), ctypes.sizeof(basic)
        )
    ):
        return None
    return (
        info.volume,
        info.index_high,
        info.index_low,
        info.attributes,
        info.size_high,
        info.size_low,
        info.creation_high,
        info.creation_low,
        info.write_high,
        info.write_low,
        int(basic.CreationTime),
        int(basic.LastWriteTime),
        int(basic.ChangeTime),
        int(basic.FileAttributes),
    )

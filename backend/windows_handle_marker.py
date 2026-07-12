from __future__ import annotations

import ctypes
import json
import os
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class HandleStat:
    volume_serial: int
    file_id: int
    size: int
    mtime_ns: int
    reparse: bool


class NativeOps(Protocol):
    def open_root(self, path: Path): ...
    def open_directory_relative(self, root, name: str): ...
    def create_file_relative(self, directory, name: str): ...
    def stat(self, handle) -> HandleStat: ...
    def write_all(self, handle, data: bytes) -> None: ...
    def flush(self, handle) -> None: ...
    def rename_relative(self, handle, directory, name: str) -> None: ...
    def delete_relative(self, directory, name: str) -> None: ...
    def close(self, handle) -> None: ...
    def read_file_relative(self, root: Path, components: tuple[str, ...]) -> bytes: ...


def write_marker(
    ops: NativeOps,
    root: Path,
    run_id: str,
    payload: dict,
) -> HandleStat:
    root_handle = directory = temp = None
    temp_name = f".reconciled.marker.{uuid.uuid4().hex}.tmp"
    renamed = False
    try:
        root_handle = ops.open_root(root)
        root_before = ops.stat(root_handle)
        if root_before.reparse:
            raise OSError("runs root is a reparse point")
        directory = ops.open_directory_relative(root_handle, run_id)
        directory_before = ops.stat(directory)
        if directory_before.reparse:
            raise OSError("run directory is a reparse point")
        temp = ops.create_file_relative(directory, temp_name)
        ops.write_all(temp, json.dumps(payload, indent=2).encode("utf-8"))
        ops.flush(temp)
        identity = lambda value: (value.volume_serial, value.file_id)
        if identity(ops.stat(directory)) != identity(directory_before) or identity(ops.stat(root_handle)) != identity(root_before):
            raise OSError("marker directory identity changed")
        ops.rename_relative(temp, directory, "reconciled.marker")
        renamed = True
        ops.flush(directory)
        marker = ops.stat(temp)
        if marker.reparse:
            raise OSError("marker became a reparse point")
        if identity(ops.stat(directory)) != identity(directory_before) or identity(ops.stat(root_handle)) != identity(root_before):
            raise OSError("marker directory identity changed after rename")
        return marker
    finally:
        if temp is not None:
            ops.close(temp)
        if not renamed and directory is not None:
            try:
                ops.delete_relative(directory, temp_name)
            except OSError:
                pass
        if directory is not None:
            ops.close(directory)
        if root_handle is not None:
            ops.close(root_handle)


def write_atomic_file(ops: NativeOps, root: Path, name: str, data: bytes) -> HandleStat:
    root_handle = temp = None
    temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
    renamed = False
    try:
        root_handle = ops.open_root(root)
        before = ops.stat(root_handle)
        if before.reparse:
            raise OSError("target directory is a reparse point")
        temp = ops.create_file_relative(root_handle, temp_name)
        ops.write_all(temp, data)
        ops.flush(temp)
        current = ops.stat(root_handle)
        if (current.volume_serial, current.file_id) != (before.volume_serial, before.file_id):
            raise OSError("target directory identity changed")
        ops.rename_relative(temp, root_handle, name)
        renamed = True
        ops.flush(root_handle)
        result = ops.stat(temp)
        if result.reparse:
            raise OSError("target became a reparse point")
        return result
    finally:
        if temp is not None:
            ops.close(temp)
        if not renamed and root_handle is not None:
            try:
                ops.delete_relative(root_handle, temp_name)
            except OSError:
                pass
        if root_handle is not None:
            ops.close(root_handle)


class WindowsNativeOps:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows native marker APIs are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll")
        self._bind()

    def _bind(self) -> None:
        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR)]
        class OBJECT_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE), ("ObjectName", ctypes.POINTER(UNICODE_STRING)), ("Attributes", wintypes.ULONG), ("SecurityDescriptor", wintypes.LPVOID), ("SecurityQualityOfService", wintypes.LPVOID)]
        class IO_STATUS_BLOCK(ctypes.Structure):
            _fields_ = [("Status", ctypes.c_ssize_t), ("Information", ctypes.c_size_t)]
        class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
            _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]
        class FILE_ID_128(ctypes.Structure):
            _fields_ = [("Identifier", ctypes.c_ubyte * 16)]
        class FILE_ID_INFO(ctypes.Structure):
            _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", FILE_ID_128)]
        class FILE_STANDARD_INFO(ctypes.Structure):
            _fields_ = [("AllocationSize", ctypes.c_longlong), ("EndOfFile", ctypes.c_longlong), ("NumberOfLinks", wintypes.DWORD), ("DeletePending", wintypes.BOOLEAN), ("Directory", wintypes.BOOLEAN)]
        class FILE_BASIC_INFO(ctypes.Structure):
            _fields_ = [("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong), ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong), ("FileAttributes", wintypes.DWORD)]
        class RENAME_INFO(ctypes.Structure):
            _fields_ = [("Flags", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE), ("FileNameLength", wintypes.ULONG), ("FileName", wintypes.WCHAR * 1)]
        self.UNICODE_STRING = UNICODE_STRING
        self.OBJECT_ATTRIBUTES = OBJECT_ATTRIBUTES
        self.IO_STATUS_BLOCK = IO_STATUS_BLOCK
        self.FILE_ATTRIBUTE_TAG_INFO = FILE_ATTRIBUTE_TAG_INFO
        self.FILE_ID_INFO = FILE_ID_INFO
        self.FILE_STANDARD_INFO = FILE_STANDARD_INFO
        self.FILE_BASIC_INFO = FILE_BASIC_INFO
        self.RENAME_INFO = RENAME_INFO
        self._kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        self._kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self._kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self._kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._ntdll.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG, ctypes.POINTER(OBJECT_ATTRIBUTES), ctypes.POINTER(IO_STATUS_BLOCK), ctypes.POINTER(ctypes.c_longlong), wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG]
        self._ntdll.NtCreateFile.restype = wintypes.LONG
        self._ntdll.NtSetInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK), wintypes.LPVOID, wintypes.ULONG, ctypes.c_int]
        self._ntdll.NtSetInformationFile.restype = wintypes.LONG
        self._ntdll.NtFlushBuffersFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK)]
        self._ntdll.NtFlushBuffersFile.restype = wintypes.LONG
        self._ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
        self._ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    def _raise_nt(self, status: int) -> None:
        if status >= 0:
            return
        code = int(self._ntdll.RtlNtStatusToDosError(status))
        raise OSError(code, ctypes.FormatError(code))

    def _relative_open(self, root, name: str, *, directory: bool, create: bool):
        if not name or name in {".", ".."} or any(c in name for c in "/\\:\0"):
            raise ValueError("invalid relative marker component")
        backing = ctypes.create_unicode_buffer(name)
        encoded_len = len(name.encode("utf-16-le"))
        us = self.UNICODE_STRING(encoded_len, encoded_len + 2, ctypes.cast(backing, wintypes.LPWSTR))
        oa = self.OBJECT_ATTRIBUTES(ctypes.sizeof(self.OBJECT_ATTRIBUTES), root, ctypes.pointer(us), 0x40, None, None)
        iosb = self.IO_STATUS_BLOCK()
        handle = wintypes.HANDLE()
        access = 0x00100000 | 0x80
        access |= (0x0001 | 0x0020 | 0x0002) if directory else ((0x40000000 if create else 0x80000000) | 0x00010000)
        options = 0x20 | 0x00200000 | (0x1 if directory else 0x40)
        status = self._ntdll.NtCreateFile(ctypes.byref(handle), access, ctypes.byref(oa), ctypes.byref(iosb), None, 0, 0x7, 2 if create else 1, options, None, 0)
        self._raise_nt(status)
        return handle

    def open_root(self, path: Path):
        handle = self._kernel32.CreateFileW(str(path), 0x00100000 | 0x80 | 0x0001 | 0x0020 | 0x0002, 0x7, None, 3, 0x02000000 | 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def open_directory_relative(self, root, name: str): return self._relative_open(root, name, directory=True, create=False)
    def create_file_relative(self, directory, name: str): return self._relative_open(directory, name, directory=False, create=True)

    def stat(self, handle) -> HandleStat:
        attr = self.FILE_ATTRIBUTE_TAG_INFO(); fid = self.FILE_ID_INFO(); standard = self.FILE_STANDARD_INFO(); basic = self.FILE_BASIC_INFO()
        for cls, value in ((9, attr), (18, fid), (1, standard), (0, basic)):
            if not self._kernel32.GetFileInformationByHandleEx(handle, cls, ctypes.byref(value), ctypes.sizeof(value)):
                raise ctypes.WinError(ctypes.get_last_error())
        file_id = int.from_bytes(bytes(fid.FileId.Identifier), "little")
        return HandleStat(int(fid.VolumeSerialNumber), file_id, int(standard.EndOfFile), int(basic.LastWriteTime) * 100, bool(attr.FileAttributes & 0x400))

    def write_all(self, handle, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + 1024 * 1024]; written = wintypes.DWORD()
            if not self._kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None): raise ctypes.WinError(ctypes.get_last_error())
            if written.value == 0: raise OSError("short Windows marker write")
            offset += written.value

    def flush(self, handle) -> None:
        iosb = self.IO_STATUS_BLOCK()
        self._raise_nt(self._ntdll.NtFlushBuffersFile(handle, ctypes.byref(iosb)))

    def rename_relative(self, handle, directory, name: str) -> None:
        raw = name.encode("utf-16-le"); offset = self.RENAME_INFO.FileName.offset; size = offset + len(raw); buf = ctypes.create_string_buffer(size)
        header = self.RENAME_INFO.from_buffer(buf); header.Flags = 0x1 | 0x2 | 0x40; header.RootDirectory = directory; header.FileNameLength = len(raw)
        ctypes.memmove(ctypes.addressof(buf) + offset, raw, len(raw)); iosb = self.IO_STATUS_BLOCK()
        self._raise_nt(self._ntdll.NtSetInformationFile(handle, ctypes.byref(iosb), buf, size, 65))

    def delete_relative(self, directory, name: str) -> None:
        handle = self._relative_open(directory, name, directory=False, create=False)
        try:
            flags = wintypes.ULONG(0x1 | 0x2); iosb = self.IO_STATUS_BLOCK()
            self._raise_nt(self._ntdll.NtSetInformationFile(handle, ctypes.byref(iosb), ctypes.byref(flags), ctypes.sizeof(flags), 64))
        finally: self.close(handle)

    def close(self, handle) -> None:
        if not self._kernel32.CloseHandle(handle): raise ctypes.WinError(ctypes.get_last_error())

    def read_file_relative(self, root: Path, components: tuple[str, ...]) -> bytes:
        if not components:
            raise ValueError("relative file path is empty")
        handles = []
        try:
            current = self.open_root(root)
            handles.append(current)
            root_identity = self.stat(current)
            if root_identity.reparse:
                raise OSError("runs root is a reparse point")
            for component in components[:-1]:
                current = self.open_directory_relative(current, component)
                handles.append(current)
                if self.stat(current).reparse:
                    raise OSError("relative directory is a reparse point")
            file_handle = self._relative_open(current, components[-1], directory=False, create=False)
            handles.append(file_handle)
            before = self.stat(file_handle)
            if before.reparse:
                raise OSError("relative file is a reparse point")
            chunks: list[bytes] = []
            while True:
                buffer = ctypes.create_string_buffer(1024 * 1024)
                read = wintypes.DWORD()
                if not self._kernel32.ReadFile(file_handle, buffer, len(buffer), ctypes.byref(read), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if read.value == 0:
                    break
                chunks.append(buffer.raw[:read.value])
            after = self.stat(file_handle)
            current_root = self.stat(handles[0])
            current_path_handle = self.open_root(root)
            handles.append(current_path_handle)
            current_path_root = self.stat(current_path_handle)
            if (
                (before.volume_serial, before.file_id, before.size, before.mtime_ns)
                != (after.volume_serial, after.file_id, after.size, after.mtime_ns)
                or (root_identity.volume_serial, root_identity.file_id)
                != (current_root.volume_serial, current_root.file_id)
                or current_path_root.reparse
                or (root_identity.volume_serial, root_identity.file_id)
                != (current_path_root.volume_serial, current_path_root.file_id)
            ):
                raise OSError("relative file changed during read")
            return b"".join(chunks)
        finally:
            for handle in reversed(handles):
                self.close(handle)

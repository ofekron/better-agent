"""Static relocatability gate for materialized POSIX executables.

materialize_sdk_launch copies raw executable bytes without any awareness of
the dynamic linker's search rules. A binary that resolves a dependency via a
path relative to its own location (Mach-O `@executable_path`/`@loader_path`,
a relative `LC_RPATH`, or ELF `$ORIGIN`) breaks once copied elsewhere: the
loader cannot find the sibling library. Observed impact is not a clean
error — on macOS the child wedges in an unkillable uninterruptible-sleep
`_dyld_start` state. Apple platform-signed binaries (identified by a
nonzero CodeDirectory `platform` byte) exhibit the same wedge when copied
out of the sealed system volume and executed, independent of their dylib
layout, so they are rejected too. This module only inspects already-copied
bytes; it never executes them.
"""

from __future__ import annotations

import struct
from pathlib import Path

from codex_execution_common import ExecutionContractError

_MH_MAGIC_64 = 0xFEEDFACF
_MH_MAGIC_32 = 0xFEEDFACE
_FAT_MAGIC = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_ELF_MAGIC = b"\x7fELF"

_LC_REQ_DYLD = 0x80000000
_LC_LOAD_DYLIB = 0xC
_LC_LOAD_WEAK_DYLIB = 0x18 | _LC_REQ_DYLD
_LC_REEXPORT_DYLIB = 0x1F | _LC_REQ_DYLD
_LC_LAZY_LOAD_DYLIB = 0x20
_LC_RPATH = 0x1C | _LC_REQ_DYLD
_LC_CODE_SIGNATURE = 0x1D
_DYLIB_LOAD_COMMANDS = frozenset((
    _LC_LOAD_DYLIB,
    _LC_LOAD_WEAK_DYLIB,
    _LC_REEXPORT_DYLIB,
    _LC_LAZY_LOAD_DYLIB,
))

_CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
_CSMAGIC_CODEDIRECTORY = 0xFADE0C02
_CSSLOT_CODEDIRECTORY = 0

_PT_DYNAMIC = 2
_PT_LOAD = 1
_DT_NEEDED = 1
_DT_RPATH = 15
_DT_RUNPATH = 29
_DT_STRTAB = 5
_DT_NULL = 0


def assert_relocatable_executable(path: Path) -> None:
    data = path.read_bytes()
    if len(data) < 4:
        return
    magic_be = struct.unpack_from(">I", data, 0)[0]
    if magic_be in (_FAT_MAGIC, _FAT_MAGIC_64):
        _assert_fat_macho_relocatable(data, magic_be)
        return
    magic_le = struct.unpack_from("<I", data, 0)[0]
    if magic_le in (_MH_MAGIC_64, _MH_MAGIC_32):
        _assert_macho_slice_relocatable(data)
        return
    if data[:4] == _ELF_MAGIC:
        _assert_elf_relocatable(data)
        return


def _reject(reason: str) -> None:
    raise ExecutionContractError(
        f"materialized executable is not relocatable: {reason}",
    )


def _malformed(kind: str) -> None:
    raise ExecutionContractError(
        f"materialized executable has a malformed {kind} image",
    )


def _assert_fat_macho_relocatable(data: bytes, magic: int) -> None:
    try:
        nfat_arch = struct.unpack_from(">I", data, 4)[0]
        offset = 8
        entry_size = 20 if magic == _FAT_MAGIC else 32
        for _ in range(nfat_arch):
            if magic == _FAT_MAGIC:
                _cputype, _cpusubtype, arch_offset, arch_size, _align = (
                    struct.unpack_from(">iiIII", data, offset)
                )
            else:
                _cputype, _cpusubtype, arch_offset, arch_size, _align, _r = (
                    struct.unpack_from(">iiQQII", data, offset)
                )
            slice_data = data[arch_offset:arch_offset + arch_size]
            if len(slice_data) != arch_size:
                _malformed("fat Mach-O")
            _assert_macho_slice_relocatable(slice_data)
            offset += entry_size
    except (struct.error, IndexError) as exc:
        raise ExecutionContractError(
            "materialized executable has a malformed fat Mach-O image",
        ) from exc


def _assert_macho_slice_relocatable(data: bytes) -> None:
    try:
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic == _MH_MAGIC_64:
            header_size = 32
            ncmds, sizeofcmds = struct.unpack_from("<II", data, 16)
        elif magic == _MH_MAGIC_32:
            header_size = 28
            ncmds, sizeofcmds = struct.unpack_from("<II", data, 16)
        else:
            _malformed("Mach-O")
            return
        if header_size + sizeofcmds > len(data):
            _malformed("Mach-O")

        rpaths: list[str] = []
        dylib_refs: list[str] = []
        code_signature: tuple[int, int] | None = None

        offset = header_size
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from("<II", data, offset)
            if cmdsize < 8 or offset + cmdsize > header_size + sizeofcmds:
                _malformed("Mach-O")
            if cmd in _DYLIB_LOAD_COMMANDS:
                name_offset = struct.unpack_from("<I", data, offset + 8)[0]
                dylib_refs.append(
                    _read_lc_str(data, offset, cmdsize, name_offset),
                )
            elif cmd == _LC_RPATH:
                path_offset = struct.unpack_from("<I", data, offset + 8)[0]
                rpaths.append(
                    _read_lc_str(data, offset, cmdsize, path_offset),
                )
            elif cmd == _LC_CODE_SIGNATURE:
                dataoff, datasize = struct.unpack_from(
                    "<II", data, offset + 8,
                )
                code_signature = (dataoff, datasize)
            offset += cmdsize
    except (struct.error, IndexError, ValueError) as exc:
        raise ExecutionContractError(
            "materialized executable has a malformed Mach-O image",
        ) from exc

    for reference in dylib_refs:
        if reference.startswith("@executable_path/"):
            _reject(f"dylib reference is @executable_path-relative: {reference}")
        if reference.startswith("@loader_path/"):
            _reject(f"dylib reference is @loader_path-relative: {reference}")
        if reference.startswith("@rpath/"):
            unsafe_rpaths = [
                rpath for rpath in rpaths
                if not rpath.startswith("/")
            ]
            if not rpaths or unsafe_rpaths:
                _reject(f"dylib reference resolves via a relative rpath: {reference}")
        elif not reference.startswith("@") and not reference.startswith("/"):
            # Not an absolute path and not one of the @-relative macros dyld
            # recognizes: dyld resolves this against the process's current
            # working directory at exec time, which is exactly as
            # location-fragile as @executable_path/@loader_path.
            _reject(f"dylib reference is a bare relative path: {reference}")

    if code_signature is not None:
        _assert_not_platform_signed(data, *code_signature)


def _read_lc_str(data: bytes, cmd_offset: int, cmdsize: int, str_offset: int) -> str:
    if str_offset < 8 or str_offset >= cmdsize:
        _malformed("Mach-O")
    start = cmd_offset + str_offset
    end = data.index(b"\x00", start, cmd_offset + cmdsize)
    return data[start:end].decode("utf-8", errors="surrogateescape")


def _assert_not_platform_signed(data: bytes, dataoff: int, datasize: int) -> None:
    blob = data[dataoff:dataoff + datasize]
    if len(blob) != datasize or len(blob) < 12:
        _malformed("code signature")
    magic, length, count = struct.unpack_from(">III", blob, 0)
    if magic != _CSMAGIC_EMBEDDED_SIGNATURE or length > len(blob):
        _malformed("code signature")
    index_offset = 12
    code_directory_offset: int | None = None
    for _ in range(count):
        if index_offset + 8 > len(blob):
            _malformed("code signature")
        slot_type, slot_offset = struct.unpack_from(
            ">II", blob, index_offset,
        )
        if slot_type == _CSSLOT_CODEDIRECTORY:
            code_directory_offset = slot_offset
        index_offset += 8
    if code_directory_offset is None:
        return
    cd = blob[code_directory_offset:]
    if len(cd) < 39:
        _malformed("code directory")
    cd_magic = struct.unpack_from(">I", cd, 0)[0]
    if cd_magic != _CSMAGIC_CODEDIRECTORY:
        _malformed("code directory")
    platform = cd[38]
    if platform != 0:
        _reject("binary carries an Apple platform code signature")


def _assert_elf_relocatable(data: bytes) -> None:
    try:
        ei_class = data[4]
        ei_data = data[5]
        if ei_class not in (1, 2) or ei_data not in (1, 2):
            _malformed("ELF")
        is64 = ei_class == 2
        endian = "<" if ei_data == 1 else ">"
        if is64:
            e_phoff, e_shoff = struct.unpack_from(endian + "QQ", data, 32)
            e_phentsize, e_phnum = struct.unpack_from(
                endian + "HH", data, 54,
            )
        else:
            e_phoff, e_shoff = struct.unpack_from(endian + "II", data, 28)
            e_phentsize, e_phnum = struct.unpack_from(
                endian + "HH", data, 42,
            )

        loads: list[tuple[int, int, int]] = []
        dynamic: tuple[int, int] | None = None
        offset = e_phoff
        for _ in range(e_phnum):
            if is64:
                p_type, _flags, p_offset, p_vaddr = struct.unpack_from(
                    endian + "IIQQ", data, offset,
                )
                p_filesz = struct.unpack_from(endian + "Q", data, offset + 32)[0]
            else:
                p_type, p_offset, p_vaddr = struct.unpack_from(
                    endian + "III", data, offset,
                )
                p_filesz = struct.unpack_from(
                    endian + "I", data, offset + 16,
                )[0]
            if p_type == _PT_DYNAMIC:
                dynamic = (p_offset, p_filesz)
            elif p_type == _PT_LOAD:
                loads.append((p_vaddr, p_offset, p_filesz))
            offset += e_phentsize
    except (struct.error, IndexError) as exc:
        raise ExecutionContractError(
            "materialized executable has a malformed ELF image",
        ) from exc

    if dynamic is None:
        return
    _assert_elf_dynamic_relocatable(data, endian, is64, dynamic, loads)


def _vaddr_to_offset(loads: list[tuple[int, int, int]], vaddr: int) -> int:
    for seg_vaddr, seg_offset, seg_filesz in loads:
        if seg_vaddr <= vaddr < seg_vaddr + seg_filesz:
            return seg_offset + (vaddr - seg_vaddr)
    _malformed("ELF")
    raise AssertionError("unreachable")


def _assert_elf_dynamic_relocatable(
    data: bytes,
    endian: str,
    is64: bool,
    dynamic: tuple[int, int],
    loads: list[tuple[int, int, int]],
) -> None:
    dyn_offset, dyn_size = dynamic
    entry_size = 16 if is64 else 8
    tag_fmt = endian + ("qq" if is64 else "ii")
    try:
        entries: list[tuple[int, int]] = []
        strtab_vaddr: int | None = None
        offset = dyn_offset
        end = dyn_offset + dyn_size
        while offset + entry_size <= end:
            tag, value = struct.unpack_from(tag_fmt, data, offset)
            if tag == _DT_NULL:
                break
            if tag == _DT_STRTAB:
                strtab_vaddr = value
            entries.append((tag, value))
            offset += entry_size
    except (struct.error, IndexError) as exc:
        raise ExecutionContractError(
            "materialized executable has a malformed ELF dynamic section",
        ) from exc

    if strtab_vaddr is None:
        return
    strtab_offset = _vaddr_to_offset(loads, strtab_vaddr)

    def read_str(str_offset: int) -> str:
        start = strtab_offset + str_offset
        end = data.index(b"\x00", start)
        return data[start:end].decode("utf-8", errors="surrogateescape")

    try:
        for tag, value in entries:
            if tag in (_DT_RPATH, _DT_RUNPATH):
                for entry in read_str(value).split(":"):
                    if not entry:
                        continue
                    if "$ORIGIN" in entry or not entry.startswith("/"):
                        _reject(f"ELF search path is relative: {entry}")
            elif tag == _DT_NEEDED:
                needed = read_str(value)
                if "/" in needed and not needed.startswith("/"):
                    _reject(f"ELF dependency uses a relative path: {needed}")
    except (struct.error, IndexError, ValueError) as exc:
        raise ExecutionContractError(
            "materialized executable has a malformed ELF string table",
        ) from exc

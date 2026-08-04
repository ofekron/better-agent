from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_binary_relocatability import (  # noqa: E402
    assert_relocatable_executable,
)

_MACHO_MAGIC_64 = 0xFEEDFACF
_FAT_MAGIC = 0xCAFEBABE
_LC_LOAD_DYLIB = 0xC
_LC_LOAD_WEAK_DYLIB = 0x80000018
_LC_RPATH = 0x8000001C
_LC_CODE_SIGNATURE = 0x1D
_CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
_CSMAGIC_CODEDIRECTORY = 0xFADE0C02


def _dylib_command(cmd: int, path: str) -> bytes:
    path_bytes = path.encode("utf-8") + b"\x00"
    header_len = 24
    total_len = header_len + len(path_bytes)
    padding = (-total_len) % 8
    total_len += padding
    return (
        struct.pack("<IIIIII", cmd, total_len, header_len, 0, 0, 0)
        + path_bytes
        + b"\x00" * padding
    )


def _rpath_command(path: str) -> bytes:
    path_bytes = path.encode("utf-8") + b"\x00"
    header_len = 12
    total_len = header_len + len(path_bytes)
    padding = (-total_len) % 8
    total_len += padding
    return (
        struct.pack("<III", _LC_RPATH, total_len, header_len)
        + path_bytes
        + b"\x00" * padding
    )


def _code_directory(platform: int) -> bytes:
    return struct.pack(
        ">IIIIIIIIIBBBBI",
        _CSMAGIC_CODEDIRECTORY,
        44,  # length
        0x20200,  # version
        0,  # flags
        0,  # hashOffset
        0,  # identOffset
        0,  # nSpecialSlots
        0,  # nCodeSlots
        0,  # codeLimit
        0,  # hashSize
        0,  # hashType
        platform,
        0,  # pageSize
        0,  # spare2
    )


def _superblob(platform: int) -> bytes:
    code_directory = _code_directory(platform)
    index_offset = 12
    code_directory_offset = index_offset + 8
    index = struct.pack(">II", 0, code_directory_offset)
    length = index_offset + len(index) + len(code_directory)
    return (
        struct.pack(">III", _CSMAGIC_EMBEDDED_SIGNATURE, length, 1)
        + index
        + code_directory
    )


def build_macho_slice(
    dylib_paths: tuple[tuple[int, str], ...] = (),
    rpaths: tuple[str, ...] = (),
    platform_signed: bool = False,
) -> bytes:
    """Build a minimal, syntactically valid thin Mach-O 64 binary for testing.

    Only the load commands assert_relocatable_executable inspects are
    populated; this is never executed, only parsed.
    """
    commands = b"".join(
        _dylib_command(cmd, path) for cmd, path in dylib_paths
    )
    commands += b"".join(_rpath_command(path) for path in rpaths)

    signature = _superblob(1 if platform_signed else 0)
    header_size = 32
    signature_command_size = 16
    ncmds = len(dylib_paths) + len(rpaths) + 1
    sizeofcmds = len(commands) + signature_command_size
    header = struct.pack(
        "<IiiIIIII",
        _MACHO_MAGIC_64, 0x0100000C, 0, 2, ncmds, sizeofcmds, 0, 0,
    )
    data_offset = header_size + sizeofcmds
    signature_command = struct.pack(
        "<IIII", _LC_CODE_SIGNATURE, signature_command_size,
        data_offset, len(signature),
    )
    return header + commands + signature_command + signature


def build_fat_macho(slices: tuple[bytes, ...]) -> bytes:
    header = struct.pack(">II", _FAT_MAGIC, len(slices))
    offset = 8 + len(slices) * 20
    entries = b""
    body = b""
    for index, slice_data in enumerate(slices):
        aligned = (offset + 7) // 8 * 8
        padding = aligned - offset
        body += b"\x00" * padding
        offset += padding
        entries += struct.pack(
            ">iiIII", 0x01000007 + index, 0, offset, len(slice_data), 3,
        )
        body += slice_data
        offset += len(slice_data)
    return header + entries + body


def _elf64_dynamic(
    root: Path,
    rpath: str | None = None,
    runpath: str | None = None,
    needed: tuple[str, ...] = (),
) -> Path:
    ehdr_size = 64
    phdr_size = 56
    dyn_offset = ehdr_size + 2 * phdr_size

    strtab = b"\x00"

    def add_str(value: str) -> int:
        nonlocal strtab
        offset = len(strtab)
        strtab += value.encode("utf-8") + b"\x00"
        return offset

    entries: list[tuple[int, int]] = []
    if rpath is not None:
        entries.append((15, add_str(rpath)))
    if runpath is not None:
        entries.append((29, add_str(runpath)))
    for library in needed:
        entries.append((1, add_str(library)))

    strtab_offset = dyn_offset + (len(entries) + 2) * 16
    entries = entries + [(5, strtab_offset), (0, 0)]
    dyn_bytes = b"".join(struct.pack("<qq", tag, value) for tag, value in entries)

    total_size = strtab_offset + len(strtab)
    load_phdr = struct.pack(
        "<IIQQQQQQ", 1, 5, 0, 0, 0, total_size, total_size, 0x1000,
    )
    dynamic_phdr = struct.pack(
        "<IIQQQQQQ", 2, 6, dyn_offset, dyn_offset, dyn_offset,
        len(dyn_bytes), len(dyn_bytes), 8,
    )
    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr = (
        e_ident
        + struct.pack("<HHI", 3, 0x3E, 1)
        + struct.pack("<Q", 0)
        + struct.pack("<Q", ehdr_size)
        + struct.pack("<Q", 0)
        + struct.pack("<I", 0)
        + struct.pack("<H", ehdr_size)
        + struct.pack("<H", phdr_size)
        + struct.pack("<H", 2)
        + struct.pack("<H", 0)
        + struct.pack("<H", 0)
        + struct.pack("<H", 0)
    )
    data = ehdr + load_phdr + dynamic_phdr + dyn_bytes + strtab
    path = root / "elf-bin"
    path.write_bytes(data)
    return path


def _assert_rejected(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bin"
        path.write_bytes(data)
        try:
            assert_relocatable_executable(path)
        except ExecutionContractError as exc:
            return str(exc)
    raise AssertionError("expected materialized executable to be rejected")


def _assert_accepted(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bin"
        path.write_bytes(data)
        assert_relocatable_executable(path)


def test_macho_rejects_executable_path_relative_dylib() -> None:
    message = _assert_rejected(
        build_macho_slice(
            dylib_paths=((_LC_LOAD_DYLIB, "@executable_path/../lib/libpython3.13.dylib"),),
        ),
    )
    assert "@executable_path" in message


def test_macho_rejects_loader_path_relative_dylib() -> None:
    _assert_rejected(
        build_macho_slice(
            dylib_paths=((_LC_LOAD_WEAK_DYLIB, "@loader_path/../lib/x.dylib"),),
        ),
    )


def test_macho_rejects_bare_relative_dylib_reference() -> None:
    message = _assert_rejected(
        build_macho_slice(dylib_paths=((_LC_LOAD_DYLIB, "lib/libfoo.dylib"),)),
    )
    assert "bare relative" in message
    _assert_rejected(
        build_macho_slice(dylib_paths=((_LC_LOAD_DYLIB, "libfoo.dylib"),)),
    )


def test_macho_accepts_absolute_dylib() -> None:
    _assert_accepted(
        build_macho_slice(dylib_paths=((_LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"),)),
    )


def test_macho_rpath_load_requires_absolute_rpath_entries() -> None:
    _assert_accepted(
        build_macho_slice(
            dylib_paths=((_LC_LOAD_DYLIB, "@rpath/libx.dylib"),),
            rpaths=("/abs/lib",),
        ),
    )
    _assert_rejected(
        build_macho_slice(
            dylib_paths=((_LC_LOAD_DYLIB, "@rpath/libx.dylib"),),
            rpaths=("@loader_path/../lib",),
        ),
    )
    _assert_rejected(
        build_macho_slice(dylib_paths=((_LC_LOAD_DYLIB, "@rpath/libx.dylib"),)),
    )


def test_macho_rejects_platform_signed_binary() -> None:
    message = _assert_rejected(
        build_macho_slice(
            dylib_paths=((_LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"),),
            platform_signed=True,
        ),
    )
    assert "platform" in message
    _assert_accepted(
        build_macho_slice(
            dylib_paths=((_LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"),),
            platform_signed=False,
        ),
    )


def test_fat_macho_checks_every_slice() -> None:
    safe = build_macho_slice(dylib_paths=((_LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"),))
    unsafe = build_macho_slice(
        dylib_paths=((_LC_LOAD_DYLIB, "@executable_path/x.dylib"),),
    )
    _assert_rejected(build_fat_macho((unsafe, safe)))
    _assert_rejected(build_fat_macho((safe, unsafe)))
    _assert_accepted(build_fat_macho((safe, safe)))


def test_malformed_macho_and_elf_are_rejected() -> None:
    _assert_rejected(b"\xcf\xfa\xed\xfe\x00")
    _assert_rejected(b"\x7fELF\x02\x01\x01" + b"\x00" * 2)


def test_non_loader_content_passes_through() -> None:
    _assert_accepted(b"#!/bin/sh\nprintf materialized\n")
    _assert_accepted(b"")


def test_elf_rejects_origin_relative_runpath() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = _elf64_dynamic(Path(raw), runpath="$ORIGIN/../lib")
        try:
            assert_relocatable_executable(path)
        except ExecutionContractError as exc:
            assert "relative" in str(exc)
        else:
            raise AssertionError("expected rejection")


def test_elf_accepts_absolute_rpath_and_runpath() -> None:
    with tempfile.TemporaryDirectory() as raw:
        assert_relocatable_executable(
            _elf64_dynamic(Path(raw), rpath="/opt/lib", runpath="/opt/lib:/opt/lib2"),
        )


def test_elf_rejects_relative_needed_but_allows_bare_name() -> None:
    with tempfile.TemporaryDirectory() as raw:
        assert_relocatable_executable(
            _elf64_dynamic(Path(raw), needed=("libc.so.6",)),
        )
    with tempfile.TemporaryDirectory() as other:
        path = _elf64_dynamic(Path(other), needed=("libs/libfoo.so",))
        try:
            assert_relocatable_executable(path)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("expected rejection")


_PE_SIG = b"PE\x00\x00"


def _build_pe(
    imports: tuple[str, ...] = (),
    delay_imports: tuple[str, ...] = (),
) -> bytes:
    """Build a minimal, syntactically valid PE32+ image for testing.

    Only the import/delay-import directories assert_relocatable_executable
    inspects are populated, with a single identity-mapped section (RVA ==
    file offset) so the RVA-to-file-offset math stays trivial. Never
    executed, only parsed.
    """
    dos_header = b"MZ" + b"\x00" * 0x3A + struct.pack("<I", 0x40)
    assert len(dos_header) == 0x40
    file_header = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 240, 0)
    section_start = 0x40 + len(_PE_SIG) + len(file_header) + 240 + 40

    import_table_size = (len(imports) + 1) * 20
    delay_table_size = (len(delay_imports) + 1) * 32 if delay_imports else 0
    names_start = import_table_size + delay_table_size

    name_rvas: dict[str, int] = {}
    cursor = names_start
    for name in (*imports, *delay_imports):
        name_rvas[name] = section_start + cursor
        cursor += len(name.encode("ascii")) + 1

    import_table = b"".join(
        struct.pack("<IIIII", 0, 0, 0, name_rvas[name], 0) for name in imports
    ) + b"\x00" * 20
    delay_table = b""
    if delay_imports:
        delay_table = b"".join(
            struct.pack("<IIIIIIII", 0, name_rvas[name], 0, 0, 0, 0, 0, 0)
            for name in delay_imports
        ) + b"\x00" * 32
    names_blob = b"".join(
        name.encode("ascii") + b"\x00" for name in (*imports, *delay_imports)
    )
    section_data = import_table + delay_table + names_blob

    data_directory = bytearray(16 * 8)
    if imports:
        struct.pack_into(
            "<II", data_directory, 8, section_start, len(import_table),
        )
    if delay_imports:
        struct.pack_into(
            "<II", data_directory, 13 * 8,
            section_start + import_table_size, len(delay_table),
        )
    optional_header = struct.pack("<H", 0x20B) + b"\x00" * 110 + bytes(data_directory)
    assert len(optional_header) == 240

    section_header = (
        b"test\x00\x00\x00\x00"
        + struct.pack(
            "<IIII", len(section_data), section_start,
            len(section_data), section_start,
        )
        + b"\x00" * 16
    )
    assert len(section_header) == 40

    return (
        dos_header + _PE_SIG + file_header + optional_header
        + section_header + section_data
    )


def test_pe_accepts_known_system_dlls() -> None:
    _assert_accepted(_build_pe(imports=(
        "KERNEL32.dll", "ntdll.dll", "fwpuclnt.dll", "WSOCK32.dll",
    )))


def test_pe_accepts_api_set_prefix() -> None:
    _assert_accepted(_build_pe(imports=("api-ms-win-core-file-l1-2-0.dll",)))


def test_pe_rejects_bundled_import() -> None:
    message = _assert_rejected(_build_pe(imports=("kernel32.dll", "pythonXY.dll")))
    assert "pythonXY.dll" in message


def test_pe_rejects_bundled_delay_import() -> None:
    message = _assert_rejected(
        _build_pe(
            imports=("kernel32.dll",),
            delay_imports=("vcruntime140.dll",),
        ),
    )
    assert "vcruntime140.dll" in message


def test_pe_with_no_imports_is_accepted() -> None:
    _assert_accepted(_build_pe())


def test_malformed_pe_is_rejected() -> None:
    _assert_rejected(b"MZ" + b"\x00" * 0x3A + struct.pack("<I", 0x40) + b"XX")


TESTS = (
    test_macho_rejects_executable_path_relative_dylib,
    test_macho_rejects_loader_path_relative_dylib,
    test_macho_rejects_bare_relative_dylib_reference,
    test_macho_accepts_absolute_dylib,
    test_macho_rpath_load_requires_absolute_rpath_entries,
    test_macho_rejects_platform_signed_binary,
    test_fat_macho_checks_every_slice,
    test_malformed_macho_and_elf_are_rejected,
    test_non_loader_content_passes_through,
    test_elf_rejects_origin_relative_runpath,
    test_elf_accepts_absolute_rpath_and_runpath,
    test_elf_rejects_relative_needed_but_allows_bare_name,
    test_pe_accepts_known_system_dlls,
    test_pe_accepts_api_set_prefix,
    test_pe_rejects_bundled_import,
    test_pe_rejects_bundled_delay_import,
    test_pe_with_no_imports_is_accepted,
    test_malformed_pe_is_rejected,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS provider binary relocatability")

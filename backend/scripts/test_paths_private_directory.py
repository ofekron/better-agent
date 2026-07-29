from __future__ import annotations

import stat
from types import SimpleNamespace

import paths


class _Directory:
    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    def is_junction(self) -> bool:
        return False


def test_secure_windows_directory_is_not_mutated() -> None:
    original_name = paths.os.name
    original_check = paths.windows_path_has_private_acl
    original_apply = paths._set_windows_private_acl
    applied: list[tuple[object, bool]] = []
    paths.os.name = "nt"
    paths.windows_path_has_private_acl = lambda _path: True
    paths._set_windows_private_acl = (
        lambda path, *, directory: applied.append((path, directory))
    )
    try:
        paths.make_private_directory(_Directory())
    finally:
        paths.os.name = original_name
        paths.windows_path_has_private_acl = original_check
        paths._set_windows_private_acl = original_apply

    assert applied == []


def test_insecure_windows_directory_is_secured_and_verified() -> None:
    original_name = paths.os.name
    original_check = paths.windows_path_has_private_acl
    original_apply = paths._set_windows_private_acl
    checks = iter((False, True))
    applied: list[tuple[object, bool]] = []
    target = _Directory()
    paths.os.name = "nt"
    paths.windows_path_has_private_acl = lambda _path: next(checks)
    paths._set_windows_private_acl = (
        lambda path, *, directory: applied.append((path, directory))
    )
    try:
        paths.make_private_directory(target)
    finally:
        paths.os.name = original_name
        paths.windows_path_has_private_acl = original_check
        paths._set_windows_private_acl = original_apply

    assert applied == [(target, True)]


def test_secure_windows_file_is_not_mutated() -> None:
    original_name = paths.os.name
    original_check = paths.windows_path_has_private_acl
    original_apply = paths._set_windows_private_acl
    applied: list[tuple[object, bool]] = []
    target = object()
    paths.os.name = "nt"
    paths.windows_path_has_private_acl = lambda _path: True
    paths._set_windows_private_acl = (
        lambda path, *, directory: applied.append((path, directory))
    )
    try:
        paths.make_private_file(target)
    finally:
        paths.os.name = original_name
        paths.windows_path_has_private_acl = original_check
        paths._set_windows_private_acl = original_apply

    assert applied == []


if __name__ == "__main__":
    test_secure_windows_directory_is_not_mutated()
    test_insecure_windows_directory_is_secured_and_verified()
    test_secure_windows_file_is_not_mutated()
    print("paths private directory regression: ok")

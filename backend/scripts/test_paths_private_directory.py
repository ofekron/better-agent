from __future__ import annotations

import stat
from types import SimpleNamespace

import paths


class _Directory:
    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    def is_junction(self) -> bool:
        return False


class _RegularFile:
    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFREG)

    def is_junction(self) -> bool:
        return False


class _Redirect:
    def __init__(self, mode: int, *, junction: bool = False) -> None:
        self._mode = mode
        self._junction = junction

    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_mode=self._mode)

    def is_junction(self) -> bool:
        return self._junction


class _PosixDirectory:
    def __init__(self, mode: int, uid: int) -> None:
        self._mode = mode
        self._uid = uid
        self.chmods: list[int] = []

    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | self._mode,
            st_uid=self._uid,
        )

    def is_junction(self) -> bool:
        return False

    def chmod(self, mode: int) -> None:
        self.chmods.append(mode)
        self._mode = mode


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
    target = _RegularFile()
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


def test_windows_private_file_rejects_non_files_without_mutation() -> None:
    original_name = paths.os.name
    original_check = paths.windows_path_has_private_acl
    original_apply = paths._set_windows_private_acl
    applied: list[tuple[object, bool]] = []
    paths.os.name = "nt"
    paths.windows_path_has_private_acl = lambda _path: True
    paths._set_windows_private_acl = (
        lambda path, *, directory: applied.append((path, directory))
    )
    invalid = (
        _Directory(),
        _Redirect(stat.S_IFLNK),
        _Redirect(stat.S_IFREG, junction=True),
    )
    try:
        for target in invalid:
            try:
                paths.make_private_file(target)
            except PermissionError:
                pass
            else:
                raise AssertionError("redirecting private file was accepted")
    finally:
        paths.os.name = original_name
        paths.windows_path_has_private_acl = original_check
        paths._set_windows_private_acl = original_apply

    assert applied == []


def test_windows_private_directory_rejects_redirects_without_mutation() -> None:
    original_name = paths.os.name
    original_check = paths.windows_path_has_private_acl
    original_apply = paths._set_windows_private_acl
    applied: list[tuple[object, bool]] = []
    paths.os.name = "nt"
    paths.windows_path_has_private_acl = lambda _path: False
    paths._set_windows_private_acl = (
        lambda path, *, directory: applied.append((path, directory))
    )
    invalid = (
        _RegularFile(),
        _Redirect(stat.S_IFLNK),
        _Redirect(stat.S_IFDIR, junction=True),
    )
    try:
        for target in invalid:
            try:
                paths.make_private_directory(target)
            except PermissionError:
                pass
            else:
                raise AssertionError("redirecting private directory was accepted")
    finally:
        paths.os.name = original_name
        paths.windows_path_has_private_acl = original_check
        paths._set_windows_private_acl = original_apply

    assert applied == []


def test_windows_directory_acl_verification_failure_is_closed() -> None:
    original_name = paths.os.name
    original_check = paths.windows_path_has_private_acl
    original_apply = paths._set_windows_private_acl
    target = _Directory()
    applied: list[tuple[object, bool]] = []
    paths.os.name = "nt"
    paths.windows_path_has_private_acl = lambda _path: False
    paths._set_windows_private_acl = (
        lambda path, *, directory: applied.append((path, directory))
    )
    try:
        try:
            paths.make_private_directory(target)
        except PermissionError:
            pass
        else:
            raise AssertionError("failed Windows ACL verification was accepted")
    finally:
        paths.os.name = original_name
        paths.windows_path_has_private_acl = original_check
        paths._set_windows_private_acl = original_apply

    assert applied == [(target, True)]


def test_posix_directory_repairs_only_current_owner() -> None:
    original_name = paths.os.name
    paths.os.name = "posix"
    current_uid = paths.os.getuid()
    insecure = _PosixDirectory(0o777, current_uid)
    secure = _PosixDirectory(0o700, current_uid)
    foreign = _PosixDirectory(0o777, current_uid + 1)
    try:
        paths.make_private_directory(insecure)
        paths.make_private_directory(secure)
        try:
            paths.make_private_directory(foreign)
        except PermissionError:
            pass
        else:
            raise AssertionError("foreign POSIX directory owner was mutated")
    finally:
        paths.os.name = original_name

    assert insecure.chmods == [0o700]
    assert secure.chmods == []
    assert foreign.chmods == []


if __name__ == "__main__":
    test_secure_windows_directory_is_not_mutated()
    test_insecure_windows_directory_is_secured_and_verified()
    test_secure_windows_file_is_not_mutated()
    test_windows_private_file_rejects_non_files_without_mutation()
    test_windows_private_directory_rejects_redirects_without_mutation()
    test_windows_directory_acl_verification_failure_is_closed()
    test_posix_directory_repairs_only_current_owner()
    print("paths private directory regression: ok")

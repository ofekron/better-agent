from __future__ import annotations

from pathlib import PurePath, PurePosixPath, PureWindowsPath


class NodePathError(ValueError):
    pass


def absolute_node_path(value: str) -> PurePath:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise NodePathError("node path must be a non-empty absolute path")
    windows = PureWindowsPath(value)
    if windows.is_absolute():
        path: PurePath = windows
    else:
        posix = PurePosixPath(value)
        if not posix.is_absolute() or "\\" in value:
            raise NodePathError(f"node path must be absolute: {value!r}")
        path = posix
    if ".." in path.parts:
        raise NodePathError(f"node path must not contain traversal: {value!r}")
    return path


def validate_cwd_roots(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise NodePathError("cwd_roots must be a list of absolute paths")
    roots: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise NodePathError("cwd_roots must be a list of absolute paths")
        absolute_node_path(value)
        roots.append(value)
    return tuple(roots)


def path_is_within_roots(value: str, roots: tuple[str, ...]) -> bool:
    path = absolute_node_path(value)
    for raw_root in roots:
        root = absolute_node_path(raw_root)
        if type(path) is type(root) and (path == root or root in path.parents):
            return True
    return False


def split_cwd_roots(raw: str, *, separator: str) -> tuple[str, ...]:
    if not raw:
        return ()
    return validate_cwd_roots([
        value
        for value in (part.strip() for part in raw.split(separator))
        if value
    ])

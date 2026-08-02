from __future__ import annotations

import pytest

from node_path_authority import (
    NodePathError,
    absolute_node_path,
    canonical_node_path,
    node_path_name,
    path_is_within_roots,
    split_cwd_roots,
    validate_cwd_roots,
)
from pathlib import PurePosixPath, PureWindowsPath


class TestAbsoluteNodePath:
    def test_posix_absolute(self) -> None:
        path = absolute_node_path("/home/user/repo")
        assert isinstance(path, PurePosixPath)
        assert path.is_absolute()

    def test_non_string_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path(123)  # type: ignore[arg-type]

    def test_empty_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path("")

    def test_none_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path(None)  # type: ignore[arg-type]

    def test_control_char_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path("/home/\x01user")

    def test_del_char_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path("/home/\x7fuser")

    def test_unit_separator_boundary_raises(self) -> None:
        # chr(31) is the highest control char rejected; boundary for `< 32`
        with pytest.raises(NodePathError):
            absolute_node_path("/home/\x1fuser")

    def test_space_char_allowed(self) -> None:
        # chr(32) is printable and must be accepted
        path = absolute_node_path("/home/user name")
        assert isinstance(path, PurePosixPath)

    def test_relative_posix_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path("relative/path")

    def test_backslash_in_posix_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path("/home/user\\repo")

    def test_traversal_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path("/home/../etc")

    def test_windows_absolute_accepted(self) -> None:
        path = absolute_node_path("C:\\Users\\repo")
        assert isinstance(path, PureWindowsPath)
        assert path.is_absolute()

    def test_windows_traversal_raises(self) -> None:
        with pytest.raises(NodePathError):
            absolute_node_path("C:\\Users\\..\\etc")


class TestCanonicalNodePath:
    def test_primary_resolves(self, tmp_path) -> None:
        real = tmp_path / "project"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        resolved = canonical_node_path(str(link), "primary")
        assert resolved == str(real.resolve())

    def test_primary_expands_user_path_component(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        resolved = canonical_node_path("~/repo", "primary")
        assert resolved == str((tmp_path / "repo").resolve())

    def test_non_primary_posix_passthrough(self) -> None:
        assert canonical_node_path("/home/user/repo", "worker-1") == "/home/user/repo"

    def test_non_primary_windows_uppercases_drive(self) -> None:
        assert canonical_node_path("c:\\Users\\repo", "worker-1") == "C:\\Users\\repo"

    def test_non_primary_windows_unc_no_drive_letter(self) -> None:
        # UNC path has no single-letter drive → returned as-is
        assert canonical_node_path("\\\\server\\share\\repo", "worker-1") == "\\\\server\\share\\repo"


class TestNodePathName:
    def test_primary_returns_basename(self) -> None:
        assert node_path_name("/home/user/myrepo", "primary") == "myrepo"

    def test_non_primary_validated_basename(self) -> None:
        assert node_path_name("/home/user/myrepo", "worker-1") == "myrepo"

    def test_invalid_returns_input_verbatim(self) -> None:
        assert node_path_name("not-a-path/../", "worker-1") == "not-a-path/../"

    def test_oserror_returns_input(self, monkeypatch) -> None:
        import node_path_authority as npa

        def boom(value: str):
            raise OSError("boom")

        monkeypatch.setattr(npa, "absolute_node_path", boom)
        assert node_path_name("/x/y", "worker-1") == "/x/y"


class TestValidateCwdRoots:
    def test_valid_roots(self) -> None:
        roots = validate_cwd_roots(["/a/b", "/c/d"])
        assert roots == ("/a/b", "/c/d")

    def test_non_list_raises(self) -> None:
        with pytest.raises(NodePathError):
            validate_cwd_roots("/a/b")  # type: ignore[arg-type]

    def test_non_string_element_raises(self) -> None:
        with pytest.raises(NodePathError):
            validate_cwd_roots(["/a/b", 5])  # type: ignore[list-item]

    def test_invalid_path_element_raises(self) -> None:
        with pytest.raises(NodePathError):
            validate_cwd_roots(["/a/b", "../etc"])


class TestPathIsWithinRoots:
    def test_within_root(self) -> None:
        roots = validate_cwd_roots(["/home/user"])
        assert path_is_within_roots("/home/user/repo", roots) is True

    def test_equals_root(self) -> None:
        roots = validate_cwd_roots(["/home/user"])
        assert path_is_within_roots("/home/user", roots) is True

    def test_outside_root(self) -> None:
        roots = validate_cwd_roots(["/home/user"])
        assert path_is_within_roots("/etc/passwd", roots) is False

    def test_type_mismatch_excluded(self) -> None:
        # posix path vs windows root → type mismatch → not within
        roots = validate_cwd_roots(["C:\\Users"])
        assert path_is_within_roots("/home/user", roots) is False


class TestSplitCwdRoots:
    def test_empty_returns_empty(self) -> None:
        assert split_cwd_roots("", separator=":") == ()

    def test_splits_and_strips(self) -> None:
        result = split_cwd_roots(" /a/b : /c/d ", separator=":")
        assert result == ("/a/b", "/c/d")

    def test_skips_blank_parts(self) -> None:
        result = split_cwd_roots("/a/b::/c/d", separator=":")
        assert result == ("/a/b", "/c/d")

    def test_comma_separator(self) -> None:
        result = split_cwd_roots("/a/b,/c/d", separator=",")
        assert result == ("/a/b", "/c/d")

    def test_invalid_part_raises(self) -> None:
        with pytest.raises(NodePathError):
            split_cwd_roots("/a/b:../etc", separator=":")

"""Unit tests for ``node_path_authority``.

Pure-logic module over ``pathlib``: absolute-path parsing/validation,
canonicalization, name extraction, and cwd-root containment. No backend
imports, no ``ba_home`` dependency at import time.

Pins:
  1. ``absolute_node_path``: non-str / empty / control-char rejection,
     Windows-absolute (drive + UNC) passthrough as ``PureWindowsPath``,
     POSIX absolute passthrough as ``PurePosixPath``, backslash-in-POSIX
     rejection, and parent-traversal (``..``) rejection on both families.
  2. ``canonical_node_path``: ``primary`` expanduser+resolve fast path,
     non-primary POSIX passthrough, Windows drive-letter uppercasing,
     UNC passthrough (no drive-letter rewrite), and invalid-path
     propagation.
  3. ``node_path_name``: ``primary`` vs non-primary name extraction and
     the ``NodePathError`` / ``ValueError`` fallback to the raw value.
  4. ``validate_cwd_roots``: type rejection, non-str element rejection,
     invalid-path propagation, and passthrough of valid lists/tuples.
  5. ``path_is_within_roots``: containment, equality, type-mismatch skip,
     mixed roots, and out-of-root ``False``.
  6. ``split_cwd_roots``: empty/whitespace -> ``()``, custom separator,
     empty-part filtering + stripping, and invalid-part propagation.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_node_path_authority_unit.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import PurePosixPath, PureWindowsPath

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-node-path-authority-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import node_path_authority as npa  # noqa: E402
from node_path_authority import (  # noqa: E402
    NodePathError,
    absolute_node_path,
    canonical_node_path,
    node_path_name,
    path_is_within_roots,
    split_cwd_roots,
    validate_cwd_roots,
)


# --- absolute_node_path -------------------------------------------------


@pytest.mark.parametrize("bad", [None, 5, [], object()])
def test_absolute_node_path_non_str_rejected(bad):
    with pytest.raises(NodePathError, match="non-empty absolute path"):
        absolute_node_path(bad)


def test_absolute_node_path_empty_rejected():
    with pytest.raises(NodePathError, match="non-empty absolute path"):
        absolute_node_path("")


@pytest.mark.parametrize("bad", ["/foo\tbar", "/foo" + chr(127), "C:\\x\x00"])
def test_absolute_node_path_control_char_rejected(bad):
    with pytest.raises(NodePathError, match="non-empty absolute path"):
        absolute_node_path(bad)


def test_absolute_node_path_posix_absolute():
    path = absolute_node_path("/usr/local/bin")
    assert isinstance(path, PurePosixPath)
    assert str(path) == "/usr/local/bin"


def test_absolute_node_path_windows_drive_absolute():
    path = absolute_node_path("C:\\Users\\me")
    assert isinstance(path, PureWindowsPath)
    assert path.is_absolute()


def test_absolute_node_path_windows_unc_absolute():
    path = absolute_node_path("\\\\server\\share\\dir")
    assert isinstance(path, PureWindowsPath)
    assert path.is_absolute()


def test_absolute_node_path_relative_rejected():
    with pytest.raises(NodePathError, match="must be absolute"):
        absolute_node_path("relative/path")


def test_absolute_node_path_backslash_in_posix_rejected():
    with pytest.raises(NodePathError, match="must be absolute"):
        absolute_node_path("/foo\\bar")


@pytest.mark.parametrize("bad", ["/foo/../bar", "C:\\x\\..\\y"])
def test_absolute_node_path_traversal_rejected(bad):
    with pytest.raises(NodePathError, match="traversal"):
        absolute_node_path(bad)


# --- canonical_node_path ------------------------------------------------


def test_canonical_node_path_primary_resolves(tmp_path):
    target = tmp_path / "sub"
    assert canonical_node_path(str(target), "primary") == str(target.resolve())


def test_canonical_node_path_posix_passthrough():
    assert canonical_node_path("/usr/local", "node-1") == "/usr/local"


def test_canonical_node_path_windows_drive_uppercased():
    assert canonical_node_path("c:\\users\\me", "node-1") == "C:\\users\\me"


def test_canonical_node_path_windows_unc_passthrough():
    assert canonical_node_path("\\\\server\\share\\x", "node-1") == "\\\\server\\share\\x"


def test_canonical_node_path_invalid_propagates():
    with pytest.raises(NodePathError):
        canonical_node_path("relative/path", "node-1")


# --- node_path_name -----------------------------------------------------


def test_node_path_name_non_primary_valid():
    assert node_path_name("/usr/local/bin", "node-1") == "bin"


def test_node_path_name_primary_unvalidated():
    # primary never validates via absolute_node_path
    assert node_path_name("foo.txt", "primary") == "foo.txt"


def test_node_path_name_non_primary_invalid_falls_back():
    assert node_path_name("relative/path", "node-1") == "relative/path"


def test_node_path_name_primary_embedded_null_falls_back():
    assert node_path_name("a\x00b", "primary") == "a\x00b"


# --- validate_cwd_roots -------------------------------------------------


@pytest.mark.parametrize("bad", ["not-a-list", 5, None, {"a": 1}])
def test_validate_cwd_roots_not_list_tuple_rejected(bad):
    with pytest.raises(NodePathError, match="must be a list of absolute paths"):
        validate_cwd_roots(bad)


def test_validate_cwd_roots_non_str_element_rejected():
    with pytest.raises(NodePathError, match="must be a list of absolute paths"):
        validate_cwd_roots(["/usr", 5])


def test_validate_cwd_roots_invalid_path_propagates():
    with pytest.raises(NodePathError, match="must be absolute"):
        validate_cwd_roots(["/usr", "relative/path"])


def test_validate_cwd_roots_valid_list_and_tuple():
    assert validate_cwd_roots(["/usr/local", "/opt"]) == ("/usr/local", "/opt")
    assert validate_cwd_roots(("/a",)) == ("/a",)


def test_validate_cwd_roots_empty():
    assert validate_cwd_roots([]) == ()


# --- path_is_within_roots -----------------------------------------------


def test_path_is_within_roots_containment():
    assert path_is_within_roots("/usr/local/bin", ("/usr/local",)) is True


def test_path_is_within_roots_equality():
    assert path_is_within_roots("/usr/local", ("/usr/local",)) is True


def test_path_is_within_roots_outside():
    assert path_is_within_roots("/etc/passwd", ("/usr/local",)) is False


def test_path_is_within_roots_type_mismatch_skips():
    # Windows value vs POSIX root -> different PurePath type -> no match
    assert path_is_within_roots("C:\\foo", ("/usr",)) is False


def test_path_is_within_roots_mixed_roots_one_matches():
    assert path_is_within_roots("/usr/local/bin", ("C:\\foo", "/usr/local")) is True


def test_path_is_within_roots_windows_within_windows():
    assert path_is_within_roots("C:\\Users\\me", ("C:\\Users",)) is True


def test_path_is_within_roots_invalid_value_propagates():
    with pytest.raises(NodePathError):
        path_is_within_roots("relative", ("/usr",))


# --- split_cwd_roots ----------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   "])
def test_split_cwd_roots_empty(raw):
    assert split_cwd_roots(raw, separator=":") == ()


def test_split_cwd_roots_custom_separator_and_stripping():
    assert split_cwd_roots("/usr/local : /opt", separator=":") == (
        "/usr/local",
        "/opt",
    )


def test_split_cwd_roots_filters_empty_parts():
    assert split_cwd_roots("/usr/local::/opt", separator=":") == (
        "/usr/local",
        "/opt",
    )


def test_split_cwd_roots_comma_separator():
    assert split_cwd_roots("/a, /b", separator=",") == ("/a", "/b")


def test_split_cwd_roots_invalid_part_propagates():
    with pytest.raises(NodePathError, match="must be absolute"):
        split_cwd_roots("/usr/local:relative", separator=":")

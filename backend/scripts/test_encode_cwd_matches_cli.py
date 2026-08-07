"""`paths.encode_cwd` must byte-for-byte match claude CLI's own
`~/.claude/projects/<token>/` directory-name encoder.

Root-cause regression for the FINAL flag-ON content bug: a worktree cwd
like `/Users/x/repo/.claude/worktrees/foo` has a `.claude` path segment.
The old `encode_cwd` only replaced `/ \\ : _` with `-`, leaving the `.`
in `.claude` untouched -- producing `-x-repo-.claude-worktrees-foo`
(single dash before "claude" and a literal dot) while claude CLI itself
(verified by extracting its own encoder, `RA()`, from the installed
CLI binary: `e.replace(/[^a-zA-Z0-9]/g,"-")`) writes to
`-x-repo--claude-worktrees-foo` (double dash -- one for `/`, one for
`.`). Every consumer that derives the transcript path from cwd
(runner.py's state.json, provider_claude.py's resume-path lookup,
native_files_manager.py's fallback resolver) computed the WRONG,
non-existent directory, so the live per-run `ClaudeJsonlTailer` AND the
backup `OwnedClaudeJsonlTailer` both watched a path claude CLI never
wrote to and dispatched zero lines -- while the turn still "succeeded"
because `complete.json` independently carries the CLI's own captured
result text, masking the miss.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_encode_cwd_matches_cli.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from paths import (  # noqa: E402
    _ENCODE_CWD_MAX_LEN,
    _js_string_hashcode32,
    _to_base36,
    encode_cwd,
)


def _reference_encode(resolved_posix: str) -> str:
    """Independent oracle for claude CLI's `RA()`: replace every ASCII
    non-alphanumeric character with `-`. Deliberately reimplemented here
    (not imported from `paths`) so this test cannot pass merely because
    production and test share a bug."""
    return re.sub(r"[^a-zA-Z0-9]", "-", resolved_posix)


def test_dotted_worktree_segment_collapses_to_double_dash() -> None:
    """The exact field-reproduction shape: a `.claude` path segment
    inside a worktree cwd. This is the case that was wrong before the
    fix (single dash + literal dot) and must now match the CLI."""
    with tempfile.TemporaryDirectory(prefix="bc-encode-cwd-") as tmp:
        cwd = os.path.join(tmp, "repo", ".claude", "worktrees", "surface-migration")
        os.makedirs(cwd)
        resolved = os.path.realpath(cwd).replace(os.sep, "/")
        expected = _reference_encode(resolved)

        got = encode_cwd(cwd)

        assert got == expected, f"{got!r} != {expected!r}"
        # The old bug produced a single dash + literal dot right before
        # "claude" instead of a double dash. Assert the double dash is
        # actually present so a regression back to the old behavior
        # (which would still coincidentally satisfy the oracle-equality
        # check above, since both sides shared the bug) is caught too.
        assert "--claude-worktrees-surface-migration" in got, got
        assert "." not in got


def test_underscore_still_collapses_to_dash() -> None:
    """Pre-existing behavior (`_` -> `-`) must survive the rewrite."""
    with tempfile.TemporaryDirectory(prefix="bc-encode-cwd-") as tmp:
        cwd = os.path.join(tmp, "my_project", "sub_dir")
        os.makedirs(cwd)
        resolved = os.path.realpath(cwd).replace(os.sep, "/")
        expected = _reference_encode(resolved)

        got = encode_cwd(cwd)

        assert got == expected, f"{got!r} != {expected!r}"
        assert "_" not in got


def test_colon_still_collapses_to_dash_windows_drive_shape() -> None:
    """Windows drive-letter shape (`C:\\foo`) must still encode with a
    double dash (`C--foo`), matching the pre-existing documented
    contract -- the rewrite must not regress this."""
    from paths import _js_ascii_alnum_dash

    assert _js_ascii_alnum_dash("C:/foo") == "C--foo"


def test_plain_path_unaffected() -> None:
    """A cwd with no special characters must encode identically to the
    pre-rewrite behavior (pure `/` -> `-` collapse). Uses the same
    full-non-alnum oracle as the other cases (not a bare `/`-only
    replace) because the OS temp dir itself may contain characters
    (e.g. an underscore in macOS's `TMPDIR`) that also need collapsing —
    this test is about the *segment under test* being plain, not about
    the ambient tmp path."""
    with tempfile.TemporaryDirectory(prefix="bc-encode-cwd-") as tmp:
        cwd = os.path.join(tmp, "plain", "project")
        os.makedirs(cwd)
        resolved = os.path.realpath(cwd).replace(os.sep, "/")
        expected = _reference_encode(resolved)

        got = encode_cwd(cwd)

        assert got == expected, f"{got!r} != {expected!r}"


def test_long_cwd_truncates_and_appends_cli_hash_suffix() -> None:
    """Past `_ENCODE_CWD_MAX_LEN` characters claude CLI truncates and
    appends `-<hash>` (`abs(art(cwd)).toString(36)`). Verify `encode_cwd`
    reproduces both the truncation point and the exact hash. Nests many
    short segments (not one long one) since a single >255-char path
    component would exceed the filesystem's own filename limit."""
    with tempfile.TemporaryDirectory(prefix="bc-encode-cwd-") as tmp:
        cwd = tmp
        for _ in range(30):
            cwd = os.path.join(cwd, "x" * 10)
        os.makedirs(cwd)
        resolved = os.path.realpath(cwd).replace(os.sep, "/")
        token = _reference_encode(resolved)
        assert len(token) > _ENCODE_CWD_MAX_LEN, "test setup must exceed the cap"
        expected_hash = _to_base36(abs(_js_string_hashcode32(resolved)))
        expected = f"{token[:_ENCODE_CWD_MAX_LEN]}-{expected_hash}"

        got = encode_cwd(cwd)

        assert got == expected, f"{got!r} != {expected!r}"
        assert len(got) == _ENCODE_CWD_MAX_LEN + 1 + len(expected_hash)


def test_js_string_hashcode32_matches_known_vector() -> None:
    """`art("")` (JS) is 0; `art("a")` is `97` (its own charCode, since
    `0*31+97=97`); `art("ab")` is `97*31+98=3105`. Locks the 32-bit
    wraparound port against silent drift."""
    assert _js_string_hashcode32("") == 0
    assert _js_string_hashcode32("a") == 97
    assert _js_string_hashcode32("ab") == 97 * 31 + 98


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-v"]))

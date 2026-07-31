"""Unit-tier coverage for file_browser.py.

Covers the functions the pytest tier did not exercise: path normalization,
raw/preview media gating, directory listing, file/dir creation, the search
helpers and search_tree, file content/metadata/write/edit reconstruction, and
the git porcelain parsers. The security-critical invariants locked here:

- Raw serving must reject HTML/JS/CSS (preview-only types) unless the caller
  explicitly opts in via allow_preview_types.
- get_git_status must bucket unmerged (DD/AA/UU/...) paths as ``conflicted``
  BEFORE the M/A/D substring checks, or a conflicted path silently lands in the
  wrong bucket.

File I/O uses pytest's tmp_path (file_browser takes explicit paths; it does not
touch BETTER_AGENT_HOME). Git parsing branches that are impractical to trigger
for real (every unmerged code) feed crafted porcelain via a subprocess stub;
the end-to-end git paths use real temporary git repos.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import file_browser
from file_browser import (
    _build_dir_tree,
    _depth_limited_walk,
    _mdfind_dirs,
    _norm,
    _ripgrep_files,
    create_directory,
    create_file,
    get_file_content,
    get_file_metadata,
    get_file_tree,
    get_git_status,
    get_git_tree,
    get_raw_file_info,
    git_commit,
    git_commit_and_push,
    list_directories,
    reconstruct_before_edit,
    search_tree,
    write_file_content,
)


# ── _norm ───────────────────────────────────────────────────────────────────

def test_norm_renders_forward_slashes():
    assert _norm(Path("a") / "b" / "c") == "a/b/c"
    assert _norm("x/y/z") == "x/y/z"


# ── get_raw_file_info: media vs preview gating ──────────────────────────────

def test_raw_file_info_media_type_and_size(tmp_path):
    f = tmp_path / "pic.PNG"
    f.write_bytes(b"\x89PNG\r\n")
    info = get_raw_file_info(str(f))
    assert info["mime_type"] == "image/png"
    assert info["size"] == len(b"\x89PNG\r\n")
    assert info["path"] == _norm(f)


def test_raw_file_info_rejects_unknown_type(tmp_path):
    f = tmp_path / "thing.xyz"
    f.write_text("nope")
    try:
        get_raw_file_info(str(f))
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown extension")


def test_raw_file_info_rejects_preview_type_unless_opted_in(tmp_path):
    """Security: HTML/JS/CSS must not be raw-servable through the plain route."""
    f = tmp_path / "page.html"
    f.write_text("<html/>")
    # Plain route rejects preview-only types.
    try:
        get_raw_file_info(str(f))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for preview-only type without opt-in")
    # Signed-preview route allows it.
    assert get_raw_file_info(str(f), allow_preview_types=True)["mime_type"] == "text/html"


def test_raw_file_info_missing_file_raises(tmp_path):
    try:
        get_raw_file_info(str(tmp_path / "absent.png"))
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


# ── list_directories ────────────────────────────────────────────────────────

def test_list_directories_empty_falls_back_to_home():
    result = list_directories("")
    assert result["exists"] is True
    assert result["path"] == _norm(Path.home())
    assert result["parent"] is not None


def test_list_directories_nonexistent_returns_exists_false(tmp_path):
    target = tmp_path / "missing" / "deep"
    result = list_directories(str(target))
    assert result["exists"] is False
    assert result["entries"] == []
    assert result["parent"] is not None


def test_list_directories_lists_only_visible_subdirs(tmp_path):
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / ".hidden_dir").mkdir()
    (tmp_path / "a_file").write_text("x")
    result = list_directories(str(tmp_path))
    assert result["exists"] is True
    names = [e["name"] for e in result["entries"]]
    assert names == ["a_dir", "b_dir"]  # sorted, hidden + files excluded
    assert all(e["path"] == _norm(tmp_path / e["name"]) for e in result["entries"])


# ── get_file_tree ───────────────────────────────────────────────────────────

def test_get_file_tree_file_root_is_file_node(tmp_path):
    f = tmp_path / "leaf.txt"
    f.write_text("x")
    tree = get_file_tree(str(f))
    assert tree["type"] == "file"
    assert tree["name"] == "leaf.txt"


def test_get_file_tree_prunes_skip_dirs_and_sorts(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "zdir").mkdir()
    (tmp_path / "adir").mkdir()
    (tmp_path / "file.txt").write_text("x")
    tree = get_file_tree(str(tmp_path), max_depth=2)
    child_names = [c["name"] for c in tree["children"]]
    # Sort key is (not is_dir, name): directories first, then files.
    assert child_names == ["adir", "zdir", "file.txt"]


def test_get_file_tree_depth_limit_marks_unloaded(tmp_path):
    deep = tmp_path / "d1"
    deep.mkdir()
    (deep / "d2").mkdir()
    tree = get_file_tree(str(tmp_path), max_depth=1)
    d1 = tree["children"][0]
    assert d1["children_loaded"] is False
    assert d1["has_more_children"] is True
    assert d1["children"] == []


# ── create_file / create_directory ──────────────────────────────────────────

def test_create_file_success(tmp_path):
    result = create_file(str(tmp_path / "new.txt"))
    assert result["type"] == "file"
    assert (tmp_path / "new.txt").is_file()


def test_create_file_existing_raises(tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("x")
    try:
        create_file(str(f))
    except FileExistsError:
        return
    raise AssertionError("expected FileExistsError")


def test_create_file_missing_parent_raises(tmp_path):
    try:
        create_file(str(tmp_path / "nope" / "new.txt"))
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


def test_create_directory_success_and_conflicts(tmp_path):
    assert create_directory(str(tmp_path / "newdir"))["type"] == "directory"
    try:
        create_directory(str(tmp_path / "newdir"))
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError on re-create")
    try:
        create_directory(str(tmp_path / "absent" / "nested"))
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError for missing parent")


# ── _ripgrep_files / _mdfind_dirs ───────────────────────────────────────────

def test_ripgrep_files_finds_content_match(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    (tmp_path / "b.txt").write_text("goodbye")
    hits = _ripgrep_files(tmp_path, "hello")
    assert hits is not None  # rg is installed in this env
    assert _norm(tmp_path / "a.txt") in hits or str(tmp_path / "a.txt") in hits
    assert not any("b.txt" in h for h in hits)


def test_ripgrep_files_returns_none_without_rg(monkeypatch):
    monkeypatch.setattr(file_browser.shutil, "which", lambda _name: None)
    assert _ripgrep_files(Path("/tmp"), "x") is None


def test_mdffind_dirs_returns_none_without_mdfind(monkeypatch):
    monkeypatch.setattr(file_browser.shutil, "which", lambda _name: None)
    assert _mdfind_dirs(Path("/tmp"), "x", 5) is None


# ── _depth_limited_walk ─────────────────────────────────────────────────────

def test_depth_limited_walk_matches_name_and_path(tmp_path):
    (tmp_path / "alpha_pkg").mkdir()
    (tmp_path / "alpha_pkg" / "nested_beta").mkdir()
    (tmp_path / "other").mkdir()
    matches, truncated = _depth_limited_walk(
        tmp_path, "alpha", {"name", "path"}, max_depth=6, max_visited=8000,
    )
    assert truncated is False
    assert any("alpha_pkg" in m for m in matches)
    assert any("nested_beta" in m for m in matches)  # "alpha" is in its relative path


def test_depth_limited_walk_truncates_on_max_visited(tmp_path):
    # One subdir keeps the walk cheap; force truncation via max_visited=1.
    (tmp_path / "sub").mkdir()
    matches, truncated = _depth_limited_walk(
        tmp_path, "zzz", {"name"}, max_depth=6, max_visited=1,
    )
    assert truncated is True


def test_depth_limited_walk_skips_search_skip_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "node_modules_match").mkdir()
    (tmp_path / "keep_match").mkdir()
    matches, _ = _depth_limited_walk(tmp_path, "match", {"name"}, max_depth=6)
    flat = "\n".join(matches)
    assert "keep_match" in flat
    assert "node_modules" not in flat


# ── _build_dir_tree ─────────────────────────────────────────────────────────

def test_build_dir_tree_prunes_to_match_ancestors(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b_match").mkdir()
    (tmp_path / "a" / "unrelated").mkdir()
    matches = [str(tmp_path / "a" / "b_match")]
    tree = _build_dir_tree(tmp_path, matches)
    assert tree["name"] == tmp_path.name
    a = tree["children"][0]
    assert a["name"] == "a"
    # Only the matching descendant is included, not the unrelated sibling.
    assert [c["name"] for c in a["children"]] == ["b_match"]


def test_build_dir_tree_skips_out_of_root_match(tmp_path):
    other = tmp_path.parent / "elsewhere_sibling"
    other.mkdir(exist_ok=True)
    try:
        tree = _build_dir_tree(tmp_path, [str(other)])
        # Out-of-root match is skipped; root node stands childless.
        assert tree["children"] == []
    finally:
        other.rmdir()


# ── search_tree ─────────────────────────────────────────────────────────────

def test_search_tree_empty_query_or_invalid_methods_return_empty(tmp_path):
    (tmp_path / "x.py").write_text("x")
    assert search_tree(str(tmp_path), "   ")["root"] is None
    assert search_tree(str(tmp_path), "x", methods=("bogus",))["root"] is None
    assert search_tree(str(tmp_path / "x.py"), "x")["root"] is None  # non-dir root


def test_search_tree_file_by_name(tmp_path):
    (tmp_path / "alpha.py").write_text("x")
    (tmp_path / "beta.py").write_text("x")
    res = search_tree(str(tmp_path), "alpha", kind="file", methods=("name",))
    assert res["count"] == 1
    assert res["root"] is not None


def test_search_tree_file_by_path(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "gamma.py").write_text("x")
    res = search_tree(str(tmp_path), "pkg", kind="file", methods=("path",))
    # The file's relative path contains "pkg".
    assert res["count"] >= 1


def test_search_tree_symbols_drop_for_dir_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(file_browser.shutil, "which", lambda name: None)
    (tmp_path / "thing").mkdir()
    res = search_tree(str(tmp_path), "thing", kind="dir", methods=("symbols", "name"))
    # symbols removed for dir; name still applies; no symbols_unavailable flag.
    assert res["symbols_unavailable"] is False


def test_search_tree_symbols_unavailable_when_rg_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(file_browser.shutil, "which", lambda _name: None)
    (tmp_path / "a.txt").write_text("needle")
    res = search_tree(str(tmp_path), "needle", kind="file", methods=("symbols",))
    assert res["symbols_unavailable"] is True
    assert res["root"] is None  # symbols was the only method and it was unavailable


def test_search_tree_dir_kind_uses_depth_walk_when_mdfind_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(file_browser.shutil, "which", lambda _name: None)
    (tmp_path / "matchdir").mkdir()
    res = search_tree(str(tmp_path), "matchdir", kind="dir", methods=("name",))
    assert res["count"] == 1
    assert res["root"] is not None


def test_search_tree_truncates_on_max_results(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}_match.py").write_text("x")
    res = search_tree(str(tmp_path), "match", kind="file", methods=("name",), max_results=2)
    assert res["count"] == 2
    assert res["truncated"] is True


# ── get_file_content / get_file_metadata / write ────────────────────────────

def test_get_file_content_language_detection(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1")
    assert get_file_content(str(f))["language"] == "python"

    df = tmp_path / "Dockerfile"
    df.write_text("FROM scratch")
    assert get_file_content(str(df))["language"] == "dockerfile"

    mf = tmp_path / "Makefile"
    mf.write_text("all:")
    assert get_file_content(str(mf))["language"] == "makefile"


def test_get_file_content_read_error_is_caught(tmp_path):
    # Reading a directory raises; get_file_content must surface it, not crash.
    result = get_file_content(str(tmp_path))
    assert result["content"].startswith("Error reading file:")


def test_get_file_metadata_fields(tmp_path):
    f = tmp_path / "m.txt"
    f.write_text("hello")
    meta = get_file_metadata(str(f))
    assert meta["size"] == 5
    assert meta["path"] == _norm(f)
    assert isinstance(meta["mtime_ns"], int)


def test_write_file_content_roundtrip(tmp_path):
    f = tmp_path / "w.txt"
    res = write_file_content(str(f), "abc")
    assert res["bytes"] == 3
    assert f.read_text() == "abc"


def test_reconstruct_before_edit_reverses_edit(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("new body")  # current = after-edit state
    res = reconstruct_before_edit(str(f), old_string="old body", new_string="new body")
    assert res["before_content"] == "old body"
    assert res["after_content"] == "new body"


def test_reconstruct_before_edit_fallback_when_new_string_absent(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("unchanged")
    res = reconstruct_before_edit(str(f), old_string="x", new_string="not-present")
    assert res["before_content"] == "unchanged"
    assert res["after_content"] == "unchanged"


# ── git helpers (real temp repos + parser stubs) ────────────────────────────

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t",
             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    (repo / "tracked.txt").write_text("v1")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "init")


def test_get_git_status_clean_and_buckets(repo_for_git):
    repo = repo_for_git
    status = get_git_status(str(repo))
    assert status["is_git"] is True
    assert status["branch"] == "main"
    assert status["modified"] == []

    # Create real dirty state across buckets.
    (repo / "tracked.txt").write_text("changed")          # modified
    (repo / "gone.txt").write_text("x"); _git(repo, "add", "gone.txt"); _git(repo, "rm", "--cached", "-q", "gone.txt")  # exercise add path then...
    (repo / "staged_new.txt").write_text("x"); _git(repo, "add", "staged_new.txt")  # added (staged)
    (repo / "untracked.txt").write_text("x")              # untracked
    status = get_git_status(str(repo))
    assert "tracked.txt" in status["modified"]
    assert "staged_new.txt" in status["added"]
    assert "untracked.txt" in status["untracked"]


def test_get_git_status_non_git_dir(tmp_path):
    assert get_git_status(str(tmp_path)) == {"is_git": False}


def test_get_git_status_unmerged_bucketed_before_substring(monkeypatch):
    """Security invariant: every unmerged XY code lands in `conflicted`,
    never in modified/added/deleted via the M/A/D substring check."""
    unmerged_lines = ["DD path/a", "AU path/b", "UD path/c",
                      "UA path/d", "DU path/e", "AA path/f", "UU path/g"]
    fake = SimpleNamespace(returncode=0,
                           stdout="## main\n" + "\n".join(unmerged_lines) + "\n")
    monkeypatch.setattr(file_browser.subprocess, "run", lambda *a, **k: fake)
    status = get_git_status("unused")
    assert sorted(status["conflicted"]) == sorted(
        ["path/a", "path/b", "path/c", "path/d", "path/e", "path/f", "path/g"])
    assert status["modified"] == []
    assert status["added"] == []
    assert status["deleted"] == []


def test_get_git_status_substring_buckets(monkeypatch):
    # Porcelain v1 is exactly `XY path` (2-char status + space + path).
    fake = SimpleNamespace(
        returncode=0,
        stdout="## main\n M file_m\nA  file_a\nD  file_d\n?? file_q\n",
    )
    monkeypatch.setattr(file_browser.subprocess, "run", lambda *a, **k: fake)
    status = get_git_status("unused")
    assert status["modified"] == ["file_m"]
    assert status["added"] == ["file_a"]
    assert status["deleted"] == ["file_d"]
    assert status["untracked"] == ["file_q"]


def test_get_git_status_exception_returns_not_git(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(file_browser.subprocess, "run", boom)
    assert get_git_status("unused") == {"is_git": False}


# ── get_git_tree ────────────────────────────────────────────────────────────

def test_get_git_tree_limit_validation(tmp_path):
    for bad in (0, 501, True, "x", None):
        try:
            get_git_tree(str(tmp_path), limit=bad)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for limit={bad!r}")


def test_get_git_tree_non_git_dir(tmp_path):
    res = get_git_tree(str(tmp_path))
    assert res == {"is_git": False, "commits": []}


def test_get_git_tree_parses_commits(repo_for_git):
    repo = repo_for_git
    _git(repo, "tag", "v1")  # add a ref to exercise refs parsing
    res = get_git_tree(str(repo), limit=10)
    assert res["is_git"] is True
    assert res["commits"] and res["commits"][0]["hash"]
    assert any("v1" in ref for ref in res["commits"][0]["refs"])
    assert res["commits"][0]["author"] == "t"
    assert res["commits"][0]["subject"] == "init"
    assert res["dirty_count"] == 0


# ── git_commit / git_commit_and_push / get_file_diff ────────────────────────

def test_git_commit_and_commit_and_push(repo_for_git):
    repo = repo_for_git
    (repo / "tracked.txt").write_text("v2")
    ok = git_commit(str(repo), "edit")
    assert ok["ok"] is True

    # Nothing left to stage -> commit fails cleanly.
    nothing = git_commit(str(repo), "noop")
    assert nothing["ok"] is False

    # Push with no configured remote -> ok False, committed True.
    (repo / "tracked.txt").write_text("v3")
    pushed = git_commit_and_push(str(repo), "edit2")
    assert pushed["ok"] is False
    assert pushed.get("committed") is True


def test_git_commit_and_push_short_circuits_on_commit_failure():
    # No repo at all: commit fails, push never attempted, no `committed` flag.
    import tempfile
    d = Path(tempfile.mkdtemp())
    try:
        res = git_commit_and_push(str(d), "msg")
        assert res["ok"] is False
        assert "committed" not in res
    finally:
        d.rmdir()


def test_get_file_diff_returns_diff(repo_for_git):
    repo = repo_for_git
    (repo / "tracked.txt").write_text("changed")
    diff = file_browser.get_file_diff("tracked.txt", str(repo))
    assert diff is not None
    assert "changed" in diff


def test_get_file_diff_non_git_returns_none(tmp_path):
    assert file_browser.get_file_diff("x", str(tmp_path)) is None


# ── subprocess error / fast-path branches ───────────────────────────────────

def test_ripgrep_files_returns_empty_set_on_timeout(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else "rg", timeout=0)
    monkeypatch.setattr(file_browser.subprocess, "run", raise_timeout)
    # shutil.which must still resolve rg so the call is attempted.
    monkeypatch.setattr(file_browser.shutil, "which", lambda _n: "/usr/bin/rg")
    assert _ripgrep_files(Path("/tmp"), "x") == set()


def test_mdffind_dirs_parses_real_subprocess_output(tmp_path, monkeypatch):
    target = tmp_path / "indexed_dir"
    target.mkdir()
    fake = SimpleNamespace(returncode=0, stdout=f"{target}\n{tmp_path / 'a_file'}\n")
    monkeypatch.setattr(file_browser.subprocess, "run", lambda *a, **k: fake)
    monkeypatch.setattr(file_browser.shutil, "which", lambda _n: "/usr/bin/mdfind")
    hits = _mdfind_dirs(tmp_path, "indexed", 5)
    # Only directory lines survive the is_dir() filter.
    assert hits == [str(target)]


def test_search_tree_dir_kind_processes_mdfind_hits(tmp_path, monkeypatch):
    match = tmp_path / "alpha_pkg"
    match.mkdir()
    # mdfind returns a hit under root; the name/path filter decides inclusion.
    monkeypatch.setattr(file_browser, "_mdfind_dirs",
                        lambda root, q, n: [str(match)])
    res = search_tree(str(tmp_path), "alpha", kind="dir", methods=("name",))
    assert res["count"] == 1
    assert res["root"] is not None


def test_search_tree_dir_kind_skips_mdfind_hit_outside_root(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside_sibling_alpha"
    outside.mkdir(exist_ok=True)
    monkeypatch.setattr(file_browser, "_mdfind_dirs",
                        lambda root, q, n: [str(outside)])
    try:
        res = search_tree(str(tmp_path), "alpha", kind="dir", methods=("name",))
        assert res["count"] == 0
        assert res["root"] is None
    finally:
        outside.rmdir()


def test_git_commit_exception_returns_not_ok(tmp_path):
    # A non-existent cwd makes subprocess.run raise -> caught -> ok:False.
    res = git_commit(str(tmp_path / "does-not-exist"), "msg")
    assert res["ok"] is False


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def repo_for_git(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo

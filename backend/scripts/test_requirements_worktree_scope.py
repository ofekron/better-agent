#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import _test_home

_STATE_HOME = _test_home.isolate("bc-test-requirements-worktrees-")
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import git_repo_info  # noqa: E402
import native_transcript_index  # noqa: E402
import requirement_context  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _setup_repo(root: Path, name: str) -> tuple[Path, Path]:
    main = root / f"{name}-main"
    sibling = root / f"{name}-sibling"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _git(main, "commit", "--allow-empty", "-m", "init")
    _git(main, "worktree", "add", str(sibling), "-b", "sibling")
    return main, sibling


def test_processor_and_evidence_scope_include_linked_worktrees(root: Path) -> None:
    main, sibling = _setup_repo(root, "project")
    subdir = sibling / "backend"
    subdir.mkdir()
    normalized, error = requirement_context._normalize_cwd_filters(
        str(subdir),
        None,
        all_projects=False,
    )
    assert error is None
    assert normalized == (
        str(subdir),
        str(main.resolve()),
        str(sibling.resolve()),
    ), normalized

    processor_scope, error = requirement_context._requirements_processor_scope(
        str(subdir),
        None,
        all_projects=False,
    )
    assert error is None
    assert processor_scope == {
        "cwd": str(subdir),
        "cwds": [str(main.resolve()), str(sibling.resolve())],
        "all_projects": False,
    }

    records = [
        {"source_key": "main", "cwd": str(main), "text": "root requirement"},
        {"source_key": "subdir", "cwd": str(main / "backend"), "text": "subdir requirement"},
        {"source_key": "sibling", "cwd": str(sibling / "frontend"), "text": "sibling requirement"},
    ]
    (main / "backend").mkdir()
    (sibling / "frontend").mkdir()
    filtered, error = requirement_context._filter_records_by_cwds(records, normalized)
    assert error is None
    assert {record["source_key"] for record in filtered} == {"main", "subdir", "sibling"}

    captured: dict = {}
    original_spec = requirement_context.get_requirements_processor_spec
    original_run_sync = requirement_context.provisioning.run_sync
    requirement_context.get_requirements_processor_spec = lambda: SimpleNamespace(
        key=requirement_context.GET_REQUIREMENTS_PROCESSOR_KEY,
        dispatch="in_process",
    )

    def capture_run_sync(_spec, _query, ctx):
        captured.update(ctx)
        return SimpleNamespace(text='{"requirements":[]}')

    requirement_context.provisioning.run_sync = capture_run_sync
    try:
        result = requirement_context._run_requirements_processor(
            query="worktree requirement",
            cwd=str(subdir),
        )
    finally:
        requirement_context.get_requirements_processor_spec = original_spec
        requirement_context.provisioning.run_sync = original_run_sync
    assert result == {"text": '{"requirements":[]}'}
    assert captured["cwd"] == str(subdir)
    assert captured["cwds"] == [str(main.resolve()), str(sibling.resolve())]


def _native_row(text: str, path: Path, cwd: Path, element_id: str) -> tuple:
    digest = element_id * 64
    return (
        text,
        str(path),
        f"sid-{element_id}",
        str(cwd),
        "codex",
        "user_prompt",
        "",
        "2026-01-01T00:00:00Z",
        "user",
        element_id,
        "1",
        digest,
        digest,
        digest,
        digest,
        digest,
        len(text),
        len(text),
    )


def test_native_evidence_includes_sibling_subdirectories_only(root: Path) -> None:
    main, sibling = _setup_repo(root, "native-project")
    unrelated, _ = _setup_repo(root, "native-unrelated")
    requested = main / "backend"
    sibling_subdir = sibling / "frontend"
    unrelated_subdir = unrelated / "frontend"
    requested.mkdir()
    sibling_subdir.mkdir()
    unrelated_subdir.mkdir()

    normalized, error = requirement_context._normalize_cwd_filters(
        str(requested),
        None,
        all_projects=False,
    )
    assert error is None

    native_transcript_index.reset_for_test()
    conn = native_transcript_index._writer_connection()
    expected_path = root / "expected-native.jsonl"
    excluded_path = root / "excluded-native.jsonl"
    native_transcript_index._insert_index_rows(
        conn,
        [
            _native_row("worktreeneedle", expected_path, sibling_subdir, "a"),
            _native_row("worktreeneedle", excluded_path, unrelated_subdir, "b"),
        ],
        str(expected_path),
    )
    conn.execute(
        "INSERT INTO native_corpus_state(key, value) VALUES "
        "('repeat_projection_status', 'ready') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.commit()
    plan = " ".join(
        str(row[-1])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT DISTINCT cwd FROM native_element_meta WHERE cwd <> ''"
        ).fetchall()
    )
    assert "COVERING INDEX native_element_meta_cwd" in plan, plan

    rows = requirement_context._native_transcript_sql_window_rows(
        native_transcript_index,
        tokens=["worktreeneedle"],
        cwds=normalized,
        limit=6,
    )
    assert {row["path"] for row in rows} == {str(expected_path)}, rows
    assert {row["cwd"] for row in rows} == {str(sibling_subdir)}, rows
    native_transcript_index.reset_for_test()


def test_resolved_scope_overflow_is_structured(root: Path) -> None:
    main, _ = _setup_repo(root, "overflow-project")
    original_common_dir = git_repo_info.repo_common_dir
    git_repo_info.repo_common_dir = lambda _cwd: str(main / ".git")
    try:
        expanded, error = requirement_context._expand_cwds_from_candidates(
            (str(main),),
            [
                str(main / f"candidate-{index}")
                for index in range(requirement_context.REQUIREMENTS_CWD_RESOLVED_LIMIT)
            ],
        )
    finally:
        git_repo_info.repo_common_dir = original_common_dir
    assert expanded == ()
    assert error == "resolved requirements cwd scope is too large"


def test_scope_never_folds_unrelated_or_non_git_directories(root: Path) -> None:
    main, sibling = _setup_repo(root, "project-two")
    unrelated, unrelated_sibling = _setup_repo(root, "unrelated")
    plain = root / "plain"
    plain.mkdir()

    normalized, error = requirement_context._normalize_cwd_filters(
        str(main),
        None,
        all_projects=False,
    )
    assert error is None
    assert normalized == (
        str(main),
        str(main.resolve()),
        str(sibling.resolve()),
    )
    assert str(unrelated.resolve()) not in normalized
    assert str(unrelated_sibling.resolve()) not in normalized

    plain_scope, error = requirement_context._normalize_cwd_filters(
        str(plain),
        None,
        all_projects=False,
    )
    assert error is None
    assert plain_scope == (str(plain),)

    empty_scope, error = requirement_context._normalize_cwd_filters(
        "",
        None,
        all_projects=False,
    )
    assert error is None
    assert empty_scope == ()

    oversized, error = requirement_context._normalize_cwd_filters(
        str(plain),
        [str(plain)] * requirement_context.REQUIREMENTS_CWD_INPUT_LIMIT,
        all_projects=False,
    )
    assert oversized == ()
    assert error == "at most 8 cwd values are allowed"

    invalid, error = requirement_context._normalize_cwd_filters(
        "bad\x00cwd",
        None,
        all_projects=False,
    )
    assert invalid == ()
    assert error == "cwd values contain unsupported control characters"


def main() -> int:
    repos = Path(tempfile.mkdtemp(prefix="bc-test-requirements-worktree-repos-"))
    try:
        git_repo_info.clear_caches()
        test_processor_and_evidence_scope_include_linked_worktrees(repos)
        git_repo_info.clear_caches()
        test_scope_never_folds_unrelated_or_non_git_directories(repos)
        git_repo_info.clear_caches()
        test_native_evidence_includes_sibling_subdirectories_only(repos)
        git_repo_info.clear_caches()
        test_resolved_scope_overflow_is_structured(repos)
        print("requirements worktree scope tests passed")
        return 0
    finally:
        native_transcript_index.reset_for_test()
        shutil.rmtree(repos, ignore_errors=True)
        shutil.rmtree(_STATE_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

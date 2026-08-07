from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import repository_alignment as alignment  # noqa: E402


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _make_remote(root: Path, name: str) -> tuple[Path, Path]:
    remote = root / f"{name}.git"
    work = root / f"{name}-source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(remote))
    return remote, work


def _commit(work: Path, name: str, text: str) -> str:
    (work / name).write_text(text, encoding="utf-8")
    _git(work, "add", name)
    _git(work, "commit", "-m", text)
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"HEAD:refs/heads/dev")
    return sha


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-repository-alignment-") as raw:
        root = Path(raw)
        public_remote, public_source = _make_remote(root, "public")
        private_remote, private_source = _make_remote(root, "private")
        public_sha = _commit(public_source, "public.txt", "public")
        (public_source / ".gitignore").write_text(
            "/backend/.venv\n",
            encoding="utf-8",
        )
        _git(public_source, "add", ".gitignore")
        _git(public_source, "commit", "-m", "ignore runtime environment")
        public_sha = _git(public_source, "rev-parse", "HEAD")
        _git(public_source, "push", "origin", "HEAD:refs/heads/dev")
        private_sha = _commit(private_source, "private.txt", "private")
        private_proxy = root / "private-proxy"
        subprocess.run(
            ["git", "clone", str(private_source), str(private_proxy)],
            check=True,
            capture_output=True,
        )
        assert alignment.repository_snapshot(
            alignment.PRIVATE_ROLE,
            private_proxy,
        )["remote_url"] == str(private_remote)
        _git(
            private_proxy,
            "remote",
            "set-url",
            "origin",
            os.path.relpath(private_source, private_proxy),
        )
        assert alignment.repository_snapshot(
            alignment.PRIVATE_ROLE,
            private_proxy,
        )["remote_url"] == str(private_remote)
        _git(
            private_proxy,
            "remote",
            "set-url",
            "origin",
            f"file://{quote(str(private_source))}",
        )
        assert alignment.repository_snapshot(
            alignment.PRIVATE_ROLE,
            private_proxy,
        )["remote_url"] == str(private_remote)
        assert alignment._file_uri_path_text(
            "file:///C:/Users/Lenovo/repo.git",
            windows=True,
        ).replace("\\", "/") == "C:/Users/Lenovo/repo.git"
        assert alignment._file_uri_path_text(
            "file://server/share/repo.git",
            windows=True,
        ).replace("\\", "/") == "//server/share/repo.git"

        cycle_a = root / "cycle-a"
        cycle_b = root / "cycle-b"
        subprocess.run(["git", "init", str(cycle_a)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(cycle_b)], check=True, capture_output=True)
        _git(cycle_a, "remote", "add", "origin", str(cycle_b))
        _git(cycle_b, "remote", "add", "origin", str(cycle_a))
        assert alignment._canonical_remote(cycle_a) in {
            str(cycle_a),
            str(cycle_b),
        }

        node_root = root / "node"
        subprocess.run(
            ["git", "clone", str(public_remote), str(node_root)],
            check=True,
            capture_output=True,
        )
        _git(node_root, "checkout", "--detach", public_sha)
        (node_root / "backend" / ".venv").mkdir(parents=True)
        assert not alignment.repository_snapshot(
            alignment.PUBLIC_ROLE,
            node_root,
        )["dirty"]

        manifest = [
            {
                "role": alignment.PUBLIC_ROLE,
                "available": True,
                "commit_sha": public_sha,
                "remote_url": str(public_remote),
                "dirty": False,
                "required": True,
            },
            {
                "role": alignment.PRIVATE_ROLE,
                "available": True,
                "commit_sha": private_sha,
                "remote_url": str(private_remote),
                "dirty": False,
                "required": True,
            },
        ]
        activated: list[tuple[str, str]] = []
        pointer = types.SimpleNamespace(
            confirm_healthy=lambda _: None,
            set_active=lambda path, request_id: activated.append((path, request_id))
        )
        sys.modules["daemonhost"] = types.SimpleNamespace(pointer=pointer)

        original_root = alignment.app_root
        original_home = alignment.ba_home
        original_prepare = alignment._prepare_runtime
        alignment.app_root = lambda: node_root
        alignment.ba_home = lambda: root / "state"
        alignment._prepare_runtime = lambda _: None
        try:
            result = alignment.align_repositories(manifest)
            assert result["restart_required"] is True
            assert len(activated) == 1
            generation = Path(activated[0][0])
            assert generation.parent.name
            assert _git(generation, "rev-parse", "HEAD") == public_sha
            assert _git(
                generation / "better-agent-private", "rev-parse", "HEAD"
            ) == private_sha
            assert _git(node_root, "rev-parse", "HEAD") == public_sha

            wrong_origin = [{**manifest[0], "remote_url": str(private_remote)}]
            assert not alignment.repositories_match(
                alignment.current_repositories(),
                wrong_origin,
            )

            (node_root / "dirty.txt").write_text("dirty", encoding="utf-8")
            try:
                alignment.align_repositories(manifest)
            except alignment.RepositoryAlignmentError as exc:
                assert "dirty" in str(exc)
            else:
                raise AssertionError("dirty node checkout was admitted")

            # desired_manifest()'s own dirty check (distinct raise site from
            # align_repositories' above) must name the actual dirty file(s)
            # so an operator doesn't have to shell in and re-run `git
            # status` themselves to find out why publishing was refused.
            try:
                alignment.desired_manifest()
            except alignment.RepositoryAlignmentError as exc:
                assert "dirty.txt" in str(exc), str(exc)
            else:
                raise AssertionError("dirty public checkout was admitted")

            packaged = [{
                **manifest[0],
                "source_kind": "packaged",
                "remote_url": "",
            }]
            assert not alignment.repositories_match(packaged, [manifest[0]])

            before = set((root / "state" / "runtime" / "generations").iterdir())
            bad = [
                manifest[0],
                {**manifest[1], "commit_sha": "f" * 40},
            ]
            try:
                alignment.align_repositories(bad)
            except alignment.RepositoryAlignmentError:
                pass
            else:
                raise AssertionError("partial generation unexpectedly activated")
            after = set((root / "state" / "runtime" / "generations").iterdir())
            assert after == before
        finally:
            alignment.app_root = original_root
            alignment.ba_home = original_home
            alignment._prepare_runtime = original_prepare


# --------------------------------------------------------------------------- #
# Focused branch coverage for repository_alignment.py.
#
# The end-to-end main() suite above drives the happy composite-generation flow
# with real git. These functions pin the branches it cannot reach: the
# attestation/activation lifecycle (pointer-driven, no git), manifest
# validation edges, error-raise sites, and pure-logic helpers. Every test that
# touches ba_home() points it at a throwaway tempdir; no production state.
# --------------------------------------------------------------------------- #


def _raises(fn, exc_type, *fragments):
    """Assert fn() raises exc_type whose message contains each fragment."""
    try:
        fn()
    except exc_type as exc:
        msg = str(exc)
        for fr in fragments:
            assert fr in msg, f"{fr!r} not in {msg!r}"
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def _make_pointer(state):
    """A fake daemonhost.pointer returning `state` from read(), recording
    confirm_healthy/revert calls. Covers every call shape used in the module."""
    calls = {"confirm_healthy": [], "revert": []}

    def read():
        return state

    def confirm_healthy(*args):
        calls["confirm_healthy"].append(args)

    def revert(*args):
        calls["revert"].append(args)

    return types.SimpleNamespace(
        read=read, confirm_healthy=confirm_healthy, revert=revert
    ), calls


@contextmanager
def _home_override(tmp: Path):
    original = alignment.ba_home
    alignment.ba_home = lambda: tmp
    try:
        yield
    finally:
        alignment.ba_home = original


@contextmanager
def _daemonhost(pointer):
    prior = sys.modules.get("daemonhost")
    sys.modules["daemonhost"] = types.SimpleNamespace(pointer=pointer)
    try:
        yield
    finally:
        if prior is None:
            sys.modules.pop("daemonhost", None)
        else:
            sys.modules["daemonhost"] = prior


# --- import-time sys.path bootstrap ----------------------------------------

def test_repo_root_inserted_into_sys_path_when_missing():
    """The repo-root sys.path insert is guarded; reloading the module with the
    repo root absent from sys.path must re-insert it (the node-side import path
    relies on this bootstrap)."""
    import importlib

    repo_root = str(alignment._REPO_ROOT)
    saved = [p for p in sys.path if p == repo_root]
    sys.path = [p for p in sys.path if p != repo_root]
    try:
        importlib.reload(alignment)
        assert repo_root in sys.path
    finally:
        importlib.reload(alignment)
        for p in saved:
            if p not in sys.path:
                sys.path.append(p)


# --- attestation / activation lifecycle ------------------------------------

def test_record_primary_attestation_noop_when_not_switching():
    with tempfile.TemporaryDirectory() as raw:
        pointer, _ = _make_pointer({"status": "active", "request_id": "r1"})
        with _home_override(Path(raw)), _daemonhost(pointer):
            alignment.record_primary_attestation(accepted=True)
            assert not (Path(raw) / "runtime").exists()


def test_record_primary_attestation_writes_when_switching():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        pointer, _ = _make_pointer({"status": "switching", "request_id": "req-123"})
        with _home_override(tmp), _daemonhost(pointer):
            alignment.record_primary_attestation(accepted=True)
            data = json.loads(
                (tmp / "runtime" / "primary_repository_attestation.json").read_text()
            )
            assert data == {"transaction_id": "req-123", "accepted": True}


def test_record_primary_attestation_switching_without_request_id_noop():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        pointer, _ = _make_pointer({"status": "switching"})
        with _home_override(tmp), _daemonhost(pointer):
            alignment.record_primary_attestation(accepted=False)
            assert not (tmp / "runtime").exists()


def test_primary_attestation_status_pending_cases():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        with _home_override(tmp):
            assert alignment.primary_attestation_status("t1") == "pending"
        (tmp / "runtime").mkdir(parents=True)
        (tmp / "runtime" / "primary_repository_attestation.json").write_text(
            json.dumps({"transaction_id": "t1", "accepted": True})
        )
        with _home_override(tmp):
            assert alignment.primary_attestation_status("other") == "pending"
            assert alignment.primary_attestation_status("") == "pending"


def test_primary_attestation_status_accepted_rejected_none():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / "runtime").mkdir(parents=True)
        path = tmp / "runtime" / "primary_repository_attestation.json"
        path.write_text(json.dumps({"transaction_id": "t1", "accepted": True}))
        with _home_override(tmp):
            assert alignment.primary_attestation_status("t1") == "accepted"
        path.write_text(json.dumps({"transaction_id": "t1", "accepted": False}))
        with _home_override(tmp):
            assert alignment.primary_attestation_status("t1") == "rejected"
        path.write_text(json.dumps({"transaction_id": "t1"}))
        with _home_override(tmp):
            assert alignment.primary_attestation_status("t1") == "rejected"


def test_finalize_node_activation_active_when_not_switching():
    pointer, calls = _make_pointer({"status": "active"})
    with _daemonhost(pointer):
        assert alignment.finalize_node_activation(Path("/run")) == "active"
        assert calls["confirm_healthy"] == [] and calls["revert"] == []


def test_finalize_node_activation_accepted_confirms():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / "runtime").mkdir(parents=True)
        (tmp / "runtime" / "primary_repository_attestation.json").write_text(
            json.dumps({"transaction_id": "tx9", "accepted": True})
        )
        pointer, calls = _make_pointer({"status": "switching", "request_id": "tx9"})
        with _home_override(tmp), _daemonhost(pointer):
            assert alignment.finalize_node_activation(Path("/run/root")) == "active"
            assert calls["confirm_healthy"] == [("/run/root", "tx9")]
            assert calls["revert"] == []


def test_finalize_node_activation_rejected_reverts():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / "runtime").mkdir(parents=True)
        (tmp / "runtime" / "primary_repository_attestation.json").write_text(
            json.dumps({"transaction_id": "tx9", "accepted": False})
        )
        pointer, calls = _make_pointer({"status": "switching", "request_id": "tx9"})
        with _home_override(tmp), _daemonhost(pointer):
            assert alignment.finalize_node_activation(Path("/run/root")) == "rejected"
            assert calls["revert"] == [
                ("primary rejected repository generation", "tx9")
            ]
            assert calls["confirm_healthy"] == []


def test_finalize_node_activation_pending_neither_acts():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        pointer, calls = _make_pointer({"status": "switching", "request_id": "tx9"})
        with _home_override(tmp), _daemonhost(pointer):
            assert alignment.finalize_node_activation(Path("/run/root")) == "pending"
            assert calls["confirm_healthy"] == [] and calls["revert"] == []


def test_expire_node_activation_false_when_not_switching():
    pointer, calls = _make_pointer({"status": "active", "active": "/run/root"})
    with _daemonhost(pointer):
        assert alignment.expire_node_activation(Path("/run/root")) is False
        assert calls["revert"] == []


def test_expire_node_activation_false_when_active_mismatch():
    pointer, calls = _make_pointer(
        {"status": "switching", "active": "/other/root", "request_id": "tx"}
    )
    with _daemonhost(pointer):
        assert alignment.expire_node_activation(Path("/run/root")) is False
        assert calls["revert"] == []


def test_expire_node_activation_true_when_matches():
    with tempfile.TemporaryDirectory() as raw:
        run_root = Path(raw) / "run"
        pointer, calls = _make_pointer(
            {
                "status": "switching",
                "active": str(run_root.resolve()),
                "request_id": "tx5",
            }
        )
        with _daemonhost(pointer):
            assert alignment.expire_node_activation(run_root) is True
            assert calls["revert"] == [
                ("primary repository attestation did not arrive", "tx5")
            ]


# --- pure helpers ----------------------------------------------------------

def test_app_root_is_repo_root():
    root = alignment.app_root()
    assert root == Path(__file__).resolve().parents[2]
    assert (root / "backend").is_dir()


def test_repository_identity_scp_url_and_plain():
    assert (
        alignment.repository_identity("git@github.com:foo/bar.git")
        == "github.com/foo/bar"
    )
    assert (
        alignment.repository_identity("https://github.com/Foo/Bar.git")
        == "github.com/Foo/Bar"
    )
    assert (
        alignment.repository_identity("HTTPS://GitLab.COM/x/y/z")
        == "gitlab.com/x/y/z"
    )
    assert alignment.repository_identity("/local/path/") == "/local/path"
    assert alignment.repository_identity("relative") == "relative"


def test_local_remote_path_none_for_schemed_and_scp():
    repo = Path("/repo")
    assert alignment._local_remote_path(repo, "https://example.com/x.git") is None
    assert alignment._local_remote_path(repo, "git@example.com:x.git") is None


def test_local_remote_path_file_uri_relative_and_absolute():
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw)
        (repo / "sub").mkdir()
        assert alignment._local_remote_path(repo, "sub") == (repo / "sub").resolve()
        deep = repo / "deep" / "target"
        deep.mkdir(parents=True)
        assert alignment._local_remote_path(repo, f"file://{deep}") == deep.resolve()
        assert alignment._local_remote_path(repo, str(deep)) == deep.resolve()


def test_canonical_remote_returns_remote_when_local_missing():
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        missing = Path(raw) / "does-not-exist"
        _git(repo, "remote", "add", "origin", str(missing))
        assert alignment._canonical_remote(repo) == str(missing)


def test_dirty_files_empty_on_git_failure():
    with tempfile.TemporaryDirectory() as raw:
        assert alignment._dirty_files(Path(raw)) == []


def test_current_repositories_packaged_when_public_unavailable(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        fake_paths = {
            alignment.PUBLIC_ROLE: tmp / "public",
            alignment.PRIVATE_ROLE: tmp / "private",
        }
        monkeypatch.setattr(alignment, "role_paths", lambda: fake_paths)
        sha = "a" * 40
        repos = alignment.current_repositories({"commit_sha": sha})
        public = repos[0]
        assert public["available"] is True
        assert public["commit_sha"] == sha
        assert public["source_kind"] == "packaged"
        assert repos[1]["available"] is False


# --- desired_manifest ------------------------------------------------------

def _patch_repos(monkeypatch, records):
    monkeypatch.setattr(alignment, "current_repositories", lambda: list(records))


def test_desired_manifest_success_public_clean_private_absent(monkeypatch):
    sha = "b" * 40
    _patch_repos(monkeypatch, [
        {"role": alignment.PUBLIC_ROLE, "available": True, "commit_sha": sha,
         "remote_url": "https://example.com/pub.git", "dirty": False},
        {"role": alignment.PRIVATE_ROLE, "available": False, "commit_sha": "",
         "remote_url": "", "dirty": False},
    ])
    manifest = alignment.desired_manifest()
    by_role = {m["role"]: m for m in manifest}
    assert by_role[alignment.PUBLIC_ROLE]["required"] is True
    assert by_role[alignment.PRIVATE_ROLE]["required"] is False


def test_desired_manifest_raises_not_available(monkeypatch):
    _patch_repos(monkeypatch, [
        {"role": alignment.PUBLIC_ROLE, "available": False, "commit_sha": "",
         "remote_url": "", "dirty": False},
    ])
    _raises(alignment.desired_manifest, alignment.RepositoryAlignmentError,
            "not available")


def test_desired_manifest_raises_invalid_sha(monkeypatch):
    _patch_repos(monkeypatch, [
        {"role": alignment.PUBLIC_ROLE, "available": True, "commit_sha": "deadbeef",
         "remote_url": "https://x", "dirty": False},
    ])
    _raises(alignment.desired_manifest, alignment.RepositoryAlignmentError,
            "invalid commit sha")


def test_desired_manifest_raises_no_remote(monkeypatch):
    sha = "c" * 40
    _patch_repos(monkeypatch, [
        {"role": alignment.PUBLIC_ROLE, "available": True, "commit_sha": sha,
         "remote_url": "", "dirty": False},
    ])
    _raises(alignment.desired_manifest, alignment.RepositoryAlignmentError,
            "no remote url")


def test_desired_manifest_raises_dirty_with_files(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        sha = "d" * 40
        _patch_repos(monkeypatch, [
            {"role": alignment.PUBLIC_ROLE, "available": True, "commit_sha": sha,
             "remote_url": "https://x", "dirty": True},
        ])
        monkeypatch.setattr(alignment, "role_paths",
                            lambda: {alignment.PUBLIC_ROLE: Path(raw)})
        monkeypatch.setattr(alignment, "_dirty_files", lambda p: ["x.txt", "y.txt"])
        _raises(alignment.desired_manifest, alignment.RepositoryAlignmentError,
                "(dirty working tree: x.txt, y.txt)")


def test_desired_manifest_raises_dirty_without_files(monkeypatch):
    sha = "e" * 40
    _patch_repos(monkeypatch, [
        {"role": alignment.PUBLIC_ROLE, "available": True, "commit_sha": sha,
         "remote_url": "https://x", "dirty": True},
    ])
    monkeypatch.setattr(alignment, "_dirty_files", lambda p: [])
    _raises(alignment.desired_manifest, alignment.RepositoryAlignmentError,
            "(dirty working tree)")


# --- repositories_match ----------------------------------------------------

_PUB_SHA = "1" * 40


def _desired(**over):
    base = {"role": alignment.PUBLIC_ROLE, "commit_sha": _PUB_SHA,
            "remote_url": "https://example.com/pub.git", "required": True}
    base.update(over)
    return base


def test_repositories_match_not_list():
    assert alignment.repositories_match({}, [_desired()]) is False


def test_repositories_match_skip_not_required():
    assert alignment.repositories_match([], [_desired(required=False)]) is True


def test_repositories_match_actual_missing():
    assert alignment.repositories_match([], [_desired()]) is False


def test_repositories_match_sha_mismatch():
    observed = [{"role": alignment.PUBLIC_ROLE, "commit_sha": "9" * 40,
                 "remote_url": "https://example.com/pub.git", "dirty": False}]
    assert alignment.repositories_match(observed, [_desired()]) is False


def test_repositories_match_dirty_actual():
    observed = [{"role": alignment.PUBLIC_ROLE, "commit_sha": _PUB_SHA,
                 "remote_url": "https://example.com/pub.git", "dirty": True}]
    assert alignment.repositories_match(observed, [_desired()]) is False


def test_repositories_match_packaged_actual():
    observed = [{"role": alignment.PUBLIC_ROLE, "commit_sha": _PUB_SHA,
                 "remote_url": "https://example.com/pub.git", "dirty": False,
                 "source_kind": "packaged"}]
    assert alignment.repositories_match(observed, [_desired()]) is False


def test_repositories_match_identity_mismatch():
    observed = [{"role": alignment.PUBLIC_ROLE, "commit_sha": _PUB_SHA,
                 "remote_url": "https://example.com/OTHER.git", "dirty": False}]
    assert alignment.repositories_match(observed, [_desired()]) is False


def test_repositories_match_full_match_true():
    observed = [{"role": alignment.PUBLIC_ROLE, "commit_sha": _PUB_SHA,
                 "remote_url": "https://example.com/pub.git", "dirty": False,
                 "source_kind": "git"}]
    assert alignment.repositories_match(observed, [_desired()]) is True


def test_repositories_match_iterates_multiple_required():
    sha2 = "2" * 40
    desired = [
        _desired(),
        {"role": alignment.PRIVATE_ROLE, "commit_sha": sha2,
         "remote_url": "git@g.com:o/p.git", "required": True},
    ]
    observed = [
        {"role": alignment.PUBLIC_ROLE, "commit_sha": _PUB_SHA,
         "remote_url": "https://example.com/pub.git", "dirty": False,
         "source_kind": "git"},
        {"role": alignment.PRIVATE_ROLE, "commit_sha": sha2,
         "remote_url": "git@g.com:o/p.git", "dirty": False, "source_kind": "git"},
    ]
    assert alignment.repositories_match(observed, desired) is True


# --- _validate_manifest ----------------------------------------------------

def test_validate_manifest_not_list():
    _raises(lambda: alignment._validate_manifest({}),
            alignment.RepositoryAlignmentError, "must be a list")


def test_validate_manifest_skips_not_required():
    sha = "3" * 40
    manifest = [
        {"role": alignment.PRIVATE_ROLE, "required": False},
        {"role": alignment.PUBLIC_ROLE, "commit_sha": sha,
         "remote_url": "https://x.git", "required": True},
    ]
    entries = alignment._validate_manifest(manifest)
    assert [e["role"] for e in entries] == [alignment.PUBLIC_ROLE]


def test_validate_manifest_entry_not_dict():
    _raises(lambda: alignment._validate_manifest(["nope"]),
            alignment.RepositoryAlignmentError, "entry must be an object")


def test_validate_manifest_invalid_entry_variants():
    sha = "3" * 40
    _raises(lambda: alignment._validate_manifest(
        [{"role": "galaxy", "commit_sha": sha, "remote_url": "https://x"}]),
        alignment.RepositoryAlignmentError, "invalid repository manifest")
    _raises(lambda: alignment._validate_manifest(
        [{"role": alignment.PUBLIC_ROLE, "commit_sha": "short",
          "remote_url": "https://x"}]),
        alignment.RepositoryAlignmentError, "invalid repository manifest")
    _raises(lambda: alignment._validate_manifest(
        [{"role": alignment.PUBLIC_ROLE, "commit_sha": sha, "remote_url": ""}]),
        alignment.RepositoryAlignmentError, "invalid repository manifest")


def test_validate_manifest_requires_public():
    sha = "3" * 40
    manifest = [{"role": alignment.PRIVATE_ROLE, "commit_sha": sha,
                 "remote_url": "https://x.git", "required": True}]
    _raises(lambda: alignment._validate_manifest(manifest),
            alignment.RepositoryAlignmentError, "public repository is required")


# --- _node_remote ----------------------------------------------------------

def _patch_snapshot(monkeypatch, snapshot):
    monkeypatch.setattr(alignment, "repository_snapshot",
                        lambda role, path: snapshot)


def test_node_remote_dirty_raises(monkeypatch):
    _patch_snapshot(monkeypatch, {"available": True, "dirty": True,
                                  "remote_url": "https://x.git"})
    _raises(lambda: alignment._node_remote(
        {"role": alignment.PUBLIC_ROLE, "remote_url": "https://x.git"}),
        alignment.RepositoryAlignmentError, "checkout is dirty")


def test_node_remote_identity_mismatch_raises(monkeypatch):
    _patch_snapshot(monkeypatch, {"available": True, "dirty": False,
                                  "remote_url": "https://other.git"})
    _raises(lambda: alignment._node_remote(
        {"role": alignment.PUBLIC_ROLE, "remote_url": "https://x.git"}),
        alignment.RepositoryAlignmentError, "identity mismatch")


def test_node_remote_available_returns_url(monkeypatch):
    _patch_snapshot(monkeypatch, {"available": True, "dirty": False,
                                  "remote_url": "https://x.git"})
    assert alignment._node_remote(
        {"role": alignment.PUBLIC_ROLE, "remote_url": "https://x.git"}
    ) == "https://x.git"


def test_node_remote_packaged_public_raises(monkeypatch):
    _patch_snapshot(monkeypatch, {"available": False})
    _raises(lambda: alignment._node_remote(
        {"role": alignment.PUBLIC_ROLE, "remote_url": "https://x.git"}),
        alignment.RepositoryAlignmentError, "packaged runtime cannot align")


def test_node_remote_private_unavailable_returns_entry_url(monkeypatch):
    _patch_snapshot(monkeypatch, {"available": False})
    assert alignment._node_remote(
        {"role": alignment.PRIVATE_ROLE, "remote_url": "https://priv.git"}
    ) == "https://priv.git"


# --- _clone_exact ----------------------------------------------------------

def test_clone_exact_clone_failure():
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "out"
        _raises(lambda: alignment._clone_exact(
            "file:///nonexistent-repo-xyz-aaa", "a" * 40, target,
            alignment.PUBLIC_ROLE),
            alignment.RepositoryAlignmentError, "clone failed")
        assert not target.exists()


def test_clone_exact_checkout_failure():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        remote, _work = _make_remote(tmp, "ck")
        target = tmp / "out"
        _raises(lambda: alignment._clone_exact(
            str(remote), "z" * 40, target, alignment.PUBLIC_ROLE),
            alignment.RepositoryAlignmentError, "checkout failed")


def test_clone_exact_attestation_failure_on_dirty(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        remote, work = _make_remote(tmp, "att")
        sha = _commit(work, "f.txt", "hi")
        target = tmp / "out"
        monkeypatch.setattr(alignment, "_dirty", lambda path: True)
        _raises(lambda: alignment._clone_exact(
            str(remote), sha, target, alignment.PUBLIC_ROLE),
            alignment.RepositoryAlignmentError, "attestation failed")


# --- _run / _prepare_runtime ----------------------------------------------

def test_run_success_no_raise():
    with tempfile.TemporaryDirectory() as raw:
        alignment._run(["true"], Path(raw), 10)


def test_run_failure_raises_with_stderr():
    with tempfile.TemporaryDirectory() as raw:
        _raises(lambda: alignment._run(
            ["sh", "-c", "echo err 1>&2; exit 3"], Path(raw), 10),
            alignment.RepositoryAlignmentError, "err")


def test_run_failure_no_output_default_message():
    with tempfile.TemporaryDirectory() as raw:
        _raises(lambda: alignment._run(["sh", "-c", "exit 1"], Path(raw), 10),
                alignment.RepositoryAlignmentError,
                "generation preparation failed")


def test_prepare_runtime_missing_tools(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        monkeypatch.setattr(alignment.shutil, "which", lambda name: None)
        _raises(lambda: alignment._prepare_runtime(Path(raw)),
                alignment.RepositoryAlignmentError, "requires uv and npm")


def test_prepare_runtime_runs_commands(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / "backend").mkdir()
        (tmp / "frontend").mkdir()
        calls = []
        monkeypatch.setattr(
            alignment.shutil, "which",
            lambda name: {"uv": "/bin/uv", "npm": "/bin/npm",
                          "python": "/bin/python"}.get(name),
        )
        monkeypatch.setattr(
            alignment, "_run", lambda cmd, cwd, timeout: calls.append((cmd, cwd))
        )
        alignment._prepare_runtime(tmp)
        assert len(calls) == 3
        assert calls[0][0][-1] == "/bin/uv"
        assert calls[0][1] == tmp / "backend"
        assert calls[1][0] == ["/bin/npm", "ci"]
        assert calls[1][1] == tmp / "frontend"
        assert calls[2][0] == ["/bin/npm", "run", "build"]


# --- align_repositories private-absent branch ------------------------------

def test_align_repositories_skips_private_when_absent(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        sha = "5" * 40
        manifest = [{"role": alignment.PUBLIC_ROLE, "commit_sha": sha,
                     "remote_url": "https://x.git", "required": True}]
        activated = []
        pointer = types.SimpleNamespace(
            confirm_healthy=lambda *a: None,
            set_active=lambda path, request_id: activated.append((path, request_id)),
        )
        monkeypatch.setattr(alignment, "ba_home", lambda: tmp)
        monkeypatch.setattr(alignment, "_node_remote", lambda entry: "https://x.git")

        def fake_clone(remote, sha_, target, role):
            (target / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (target / ".git" / "info" / "exclude").write_text("")

        monkeypatch.setattr(alignment, "_clone_exact", fake_clone)
        monkeypatch.setattr(alignment, "_prepare_runtime", lambda root: None)
        monkeypatch.setattr(alignment, "app_root", lambda: tmp / "apprun")
        with _daemonhost(pointer):
            result = alignment.align_repositories(manifest)
        assert result["status"] == "staged"
        assert result["restart_required"] is True
        assert alignment.PRIVATE_ROLE not in result["changed_roles"]
        assert len(activated) == 1
        gen = Path(activated[0][0])
        assert (gen / ".git" / "info" / "exclude").read_text().endswith(
            "better-agent-private/\n"
        )
        assert not (gen / "better-agent-private").exists()


def test_repository_alignment_suite() -> None:
    """pytest entry point. `main()`'s assertions previously only ran via
    `python scripts/test_repository_alignment.py` — this file defines no
    `test_*`/`Test*` symbol, so `conftest.py`'s `pytest_ignore_collect`
    skipped it entirely under pytest/CI."""
    main()


if __name__ == "__main__":
    main()
    print("PASS repository alignment stages one exact composite generation")

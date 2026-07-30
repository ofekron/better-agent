from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
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


if __name__ == "__main__":
    main()
    print("PASS repository alignment stages one exact composite generation")

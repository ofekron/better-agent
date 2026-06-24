"""Publish a Better Agent desktop build to a `tufup` update repository.

GUI-independent and importable (used by tests and the build scripts).

A release repository has this on-disk layout, served as-is over HTTP at
`BA_UPDATE_URL`:
    <repo>/metadata/   TUF metadata (root/targets/snapshot/timestamp)
    <repo>/targets/    the release archives (+ patches)

The signing keys live in a SEPARATE keystore dir that is NEVER served
and NEVER committed (it holds private keys). The public trust anchor
`metadata/root.json` is exported and shipped inside the app bundle as
`desktop/tufup_root.json` so a fresh install can bootstrap trust.

Usage (one-time init, then once per release):
    repo = ReleaseRepo(repo_dir, keys_dir)
    repo.initialize()                      # first time only
    repo.publish_bundle(bundle_dir, version)
    repo.export_trusted_root("desktop/tufup_root.json")

`bundle_dir` is the PyInstaller onedir output (the folder tufup archives
and later moves into place on the client).

Keys are generated UNENCRYPTED — fine for a local/dev repo. For a public
production repo, generate encrypted keys and keep the keystore offline.
"""

from __future__ import annotations

from contextlib import chdir, contextmanager
from pathlib import Path

from tufup.repo import Repository

from updater import APP_NAME


class ReleaseRepo:
    """Thin wrapper over `tufup.repo.Repository` that pins the stray
    `.tufup-repo-config` file (tufup writes it into the process CWD)
    beside the repository dir instead of polluting the caller's CWD."""

    def __init__(self, repo_dir: Path | str, keys_dir: Path | str) -> None:
        self.repo_dir = Path(repo_dir)
        self.keys_dir = Path(keys_dir)
        self._repo = Repository(
            app_name=APP_NAME,
            repo_dir=str(self.repo_dir),
            keys_dir=str(self.keys_dir),
        )

    @contextmanager
    def _pinned_cwd(self):
        # tufup's save_config() writes `.tufup-repo-config` relative to
        # CWD with no override hook; run its ops from the repo's parent so
        # the config lands next to repository/ + keystore/.
        self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
        with chdir(self.repo_dir.parent):
            yield

    def initialize(self) -> None:
        """Generate the four role keys and write initial metadata. Run
        ONCE per repository."""
        with self._pinned_cwd():
            self._repo.initialize()

    def publish_bundle(self, bundle_dir: Path | str, version: str) -> None:
        """Archive `bundle_dir` as `version`, then re-sign and write the
        targets/snapshot/timestamp metadata so clients discover it."""
        with self._pinned_cwd():
            # A fresh Repository (separate-process release) has roles=None
            # until loaded from disk; in-process after initialize() they're
            # already set. Load on demand so both paths work.
            if self._repo.roles is None:
                self._repo._load_keys_and_roles(create_keys=False)
            self._repo.add_bundle(
                new_bundle_dir=str(bundle_dir), new_version=version,
            )
            self._repo.publish_changes(
                private_key_dirs=[str(self.keys_dir)],
            )

    def export_trusted_root(self, dest: Path | str) -> Path:
        """Copy `metadata/root.json` to `dest` — the public trust anchor
        shipped with the app (`desktop/tufup_root.json`)."""
        src = self.repo_dir / "metadata" / "root.json"
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        return dest


def _main(argv: list[str] | None = None) -> int:
    """CLI for the build scripts / manual releases.

        release.py init        <repo_dir> <keys_dir>
        release.py export-root <repo_dir> <keys_dir> <dest>
        release.py publish     <repo_dir> <keys_dir> <bundle_dir> <version> \
                               [--export-root <path>]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Better Agent update repo")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create a new repository")
    p_init.add_argument("repo_dir")
    p_init.add_argument("keys_dir")

    p_root = sub.add_parser("export-root", help="export trusted root.json")
    p_root.add_argument("repo_dir")
    p_root.add_argument("keys_dir")
    p_root.add_argument("dest")

    p_pub = sub.add_parser("publish", help="publish a built bundle")
    p_pub.add_argument("repo_dir")
    p_pub.add_argument("keys_dir")
    p_pub.add_argument("bundle_dir")
    p_pub.add_argument("version")
    p_pub.add_argument("--export-root", default=None,
                       help="also write the trusted root.json here")

    args = parser.parse_args(argv)
    repo = ReleaseRepo(args.repo_dir, args.keys_dir)
    if args.cmd == "init":
        repo.initialize()
        return 0
    if args.cmd == "export-root":
        repo.export_trusted_root(args.dest)
        return 0
    repo.publish_bundle(args.bundle_dir, args.version)
    if args.export_root:
        repo.export_trusted_root(args.export_root)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))

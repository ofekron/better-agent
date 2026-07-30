#!/usr/bin/env python3
"""Cut a Better Agent release, repeatably.

Default behaviour is a DRY RUN: it validates every release precondition,
builds the source artifact into a temporary directory to prove it is clean,
prints the release notes body, and prints the exact `git tag -s` /
`gh release create` commands for the operator. It mutates nothing.

Git and GitHub are only touched when explicitly asked for with `--tag` /
`--publish`. Never needs sudo; never prints environment or credentials.

Typical flow (see RELEASE.md for the authoritative walkthrough):

    scripts/release.py 0.2.0 --set-version   # bump package.json, then commit
    scripts/release.py 0.2.0                 # dry run: validate + preview
    scripts/release.py 0.2.0 --tag           # create the signed tag
    scripts/release.py 0.2.0 --build         # write dist artifacts from tag
    scripts/release.py 0.2.0 --publish       # gh release create (explicit)
    scripts/release.py 0.2.0 --emit-formula  # fill the Homebrew tap formula
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_policy import (  # noqa: E402
    PolicyError,
    archive_prefix,
    audit_paths,
    read_repo_version,
    source_archive_name,
    tag_for_version,
    validate_version,
    write_repo_version,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO_SLUG = "ofekron/better-agent"
FORMULA_TEMPLATE = REPO_ROOT / "packaging" / "homebrew" / "better-agent.rb"
VERSION_PLACEHOLDER = "__RELEASE_VERSION__"
SHA256_PLACEHOLDER = "__RELEASE_SHA256__"
MANIFEST_NAME = "SHA256SUMS"
NOTES_NAME = "RELEASE_NOTES.md"


class ReleaseError(Exception):
    pass


def fail(message: str) -> None:
    raise ReleaseError(message)


def git(*args: str, capture: bool = True) -> str:
    """Run git with argv (never a shell string) inside the repo."""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=capture,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        fail(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout if capture else ""


def git_bytes(*args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        fail(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def require_git_repo() -> None:
    if not (REPO_ROOT / ".git").exists():
        fail(f"{REPO_ROOT} is not a git checkout")


def tag_exists(tag: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def require_clean_tree() -> None:
    dirty = git("status", "--porcelain").strip()
    if dirty:
        count = len(dirty.splitlines())
        fail(
            f"working tree is not clean ({count} modified/untracked path(s)). "
            "RELEASE.md forbids releasing from a dirty tree. Commit or stash, then retry."
        )


def audit_ref(ref: str) -> None:
    listing = git("ls-tree", "-r", "--name-only", ref)
    violations = audit_paths(listing.splitlines())
    if violations:
        shown = "\n  - ".join(violations[:20])
        extra = "" if len(violations) <= 20 else f"\n  ... and {len(violations) - 20} more"
        fail(
            "release-policy exclusions violated at "
            f"{ref} ({len(violations)} path(s)):\n  - {shown}{extra}"
        )


def build_source_archive(ref: str, version: str, dest_dir: Path) -> Path:
    """Reproducible source tarball built only from tracked content at `ref`.

    `git archive` can only emit tracked files, so nothing untracked, ignored,
    or locally modified can leak in. The tar stream is gzipped with mtime=0 so
    two builds of the same ref are byte-identical.
    """
    raw_tar = git_bytes(
        "archive", "--format=tar", f"--prefix={archive_prefix(version)}", ref
    )
    out = dest_dir / source_archive_name(version)
    with out.open("wb") as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(raw_tar)
    verify_archive_members(out, version)
    return out


def verify_archive_members(archive: Path, version: str) -> None:
    """Re-audit the produced artifact, and reject any escape from the prefix."""
    prefix = archive_prefix(version)
    root = prefix.rstrip("/")
    members: list[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        for name in tar.getnames():
            if name == root:
                continue  # the archive's own root directory entry
            if not name.startswith(prefix):
                fail(f"{archive.name} contains member outside {prefix!r}: {name}")
            members.append(name[len(prefix):])
    violations = audit_paths(members)
    if violations:
        shown = "\n  - ".join(violations[:20])
        fail(f"built archive violates release policy:\n  - {shown}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_extra_artifact(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        fail(f"--artifact {raw!r} is not an existing regular file")
    return path


def write_manifest(dest_dir: Path, artifacts: list[Path]) -> Path:
    lines = [f"{sha256_file(a)}  {a.name}\n" for a in sorted(artifacts, key=lambda p: p.name)]
    manifest = dest_dir / MANIFEST_NAME
    manifest.write_text("".join(lines), encoding="utf-8")
    return manifest


def render_notes(version: str, tag: str, commit: str, artifacts: list[Path], slug: str) -> str:
    rows = [
        f"| `{a.name}` | `{sha256_file(a)}` |"
        for a in sorted(artifacts, key=lambda p: p.name)
    ]
    return "\n".join(
        [
            f"# Better Agent {tag}",
            "",
            "## Provenance",
            "",
            f"- Source commit: `{commit}`",
            f"- Tag: `{tag}` (signed annotated tag)",
            f"- Repository: `{slug}`",
            "- Built with: `scripts/release.py --build` (`git archive` from the tag,"
            " gzip mtime=0 — reproducible)",
            "",
            "## Artifacts and SHA-256 checksums",
            "",
            "| Artifact | SHA-256 |",
            "| --- | --- |",
            *rows,
            "",
            f"`{MANIFEST_NAME}` in this release lists the same digests in"
            " `shasum -a 256 -c` format:",
            "",
            "```",
            f"shasum -a 256 -c {MANIFEST_NAME}",
            "```",
            "",
            "## Install",
            "",
            "```",
            "brew install ofekron/better-agent/better-agent && better-agent-setup",
            "```",
            "",
            "```",
            f"curl -fsSL https://raw.githubusercontent.com/{slug}/{tag}/scripts/bootstrap.sh | bash",
            "```",
            "",
        ]
    )


def emit_formula(version: str, dest_dir: Path, archive: Path) -> Path:
    if not FORMULA_TEMPLATE.is_file():
        fail(f"missing Homebrew formula template at {FORMULA_TEMPLATE}")
    template = FORMULA_TEMPLATE.read_text(encoding="utf-8")
    for placeholder in (VERSION_PLACEHOLDER, SHA256_PLACEHOLDER):
        if placeholder not in template:
            fail(f"formula template no longer contains {placeholder}")
    filled = template.replace(VERSION_PLACEHOLDER, version).replace(
        SHA256_PLACEHOLDER, sha256_file(archive)
    )
    out = dest_dir / "better-agent.rb"
    out.write_text(filled, encoding="utf-8")
    return out


def output_dir(version: str) -> Path:
    return REPO_ROOT / "dist" / "release" / tag_for_version(version)


# Files produced in the output dir that are NOT GitHub release assets:
# the notes are passed via --notes-file, and the formula belongs to the tap
# repository, not to the release.
NON_ASSET_NAMES = frozenset({NOTES_NAME, "better-agent.rb"})


def release_assets(out_dir: Path) -> list[Path]:
    return sorted(
        p for p in out_dir.iterdir() if p.is_file() and p.name not in NON_ASSET_NAMES
    )


def print_operator_commands(
    version: str, tag: str, out_dir: Path, asset_names: list[str], slug: str
) -> None:
    rel = out_dir.relative_to(REPO_ROOT)
    asset_args = " ".join(f"{rel}/{name}" for name in sorted(asset_names))
    print("\n=== Operator commands (this script never runs these implicitly) ===\n")
    print("1. Create the signed tag:")
    print(f"   git tag -s {tag} -m 'Better Agent {tag}'")
    print(f"   (or: scripts/release.py {version} --tag)")
    print("\n2. Push the tag:")
    print(f"   git push github {tag}")
    print("\n3. Build the release artifacts from the tag:")
    print(f"   scripts/release.py {version} --build")
    print("\n4. Publish the GitHub release:")
    print(
        f"   gh release create {tag} --repo {slug} "
        f"--title 'Better Agent {tag}' --notes-file {rel}/{NOTES_NAME} {asset_args}"
    )
    print(f"   (or: scripts/release.py {version} --publish)")
    print("\n5. Bump the Homebrew tap:")
    print(f"   scripts/release.py {version} --emit-formula")
    print(f"   cp {rel}/better-agent.rb <tap-checkout>/Formula/better-agent.rb")
    print("   # then commit + push the tap repo\n")


def run_tag(version: str, tag: str) -> None:
    if tag_exists(tag):
        fail(f"tag {tag} already exists; refusing to move a published tag")
    print(f"release: creating signed tag {tag}")
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "tag", "-s", tag, "-m", f"Better Agent {tag}"],
        check=True,
    )
    print(f"release: created {tag}. Push it with: git push github {tag}")


def run_publish(tag: str, out_dir: Path, slug: str) -> None:
    if shutil.which("gh") is None:
        fail("gh CLI not found on PATH; run the printed gh command manually")
    notes = out_dir / NOTES_NAME
    if not notes.is_file():
        fail(f"missing {notes}; run --build first")
    assets = release_assets(out_dir)
    if not assets:
        fail(f"no artifacts in {out_dir}; run --build first")
    cmd = [
        "gh", "release", "create", tag,
        "--repo", slug,
        "--title", f"Better Agent {tag}",
        "--notes-file", str(notes),
        *[str(a) for a in assets],
    ]
    print(f"release: gh release create {tag} ({len(assets)} asset(s))")
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/release.py",
        description=(
            "Cut a Better Agent release. Dry run by default: nothing is "
            "mutated unless --set-version / --build / --tag / --publish / "
            "--emit-formula is given."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("version", help="release version, strict semver, e.g. 0.2.0")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit dry run (this is also the default when no action flag is given)",
    )
    parser.add_argument(
        "--set-version",
        action="store_true",
        help="write the version into package.json (the in-repo version mirror), then exit (no git)",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="write source artifact + SHA256SUMS + notes into dist/release/<tag>/ (requires the tag)",
    )
    parser.add_argument(
        "--tag", action="store_true", help="execute `git tag -s <tag>` (explicit git mutation)"
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="execute `gh release create` for the built artifacts (explicit GitHub mutation)",
    )
    parser.add_argument(
        "--emit-formula",
        action="store_true",
        help="write dist/release/<tag>/better-agent.rb with version + sha256 filled in",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="PATH",
        help="extra pre-built artifact (desktop DMG, APK, ...) to checksum and attach; repeatable",
    )
    parser.add_argument(
        "--repo-slug",
        default=DEFAULT_REPO_SLUG,
        help=f"GitHub owner/name for release URLs (default: {DEFAULT_REPO_SLUG})",
    )
    parser.add_argument(
        "--allow-version-drift",
        action="store_true",
        help="proceed even if the in-repo version mirror disagrees with the requested version",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        version = validate_version(args.version)
        require_git_repo()
        tag = tag_for_version(version)

        if args.set_version:
            write_repo_version(REPO_ROOT, version)
            print(f"release: set version {version} in package.json (not committed)")
            print("release: commit it, then re-run without --set-version")
            return 0

        repo_version = read_repo_version(REPO_ROOT)
        if repo_version != version and not args.allow_version_drift:
            fail(
                f"version drift: package.json says {repo_version!r} but {version!r} "
                f"was requested. Run: scripts/release.py {version} --set-version, "
                "commit, then retry (or pass --allow-version-drift)."
            )

        require_clean_tree()

        if args.tag:
            run_tag(version, tag)
            return 0

        mutating = args.build or args.emit_formula or args.publish
        if mutating and not tag_exists(tag):
            fail(f"tag {tag} does not exist. Create it first: scripts/release.py {version} --tag")

        ref = tag if tag_exists(tag) else "HEAD"
        if ref == "HEAD":
            print(f"release: tag {tag} not present yet — previewing from HEAD")
        commit = git("rev-parse", ref).strip()
        audit_ref(ref)
        print(f"release: policy audit passed for {ref} ({commit[:12]})")

        extras = [resolve_extra_artifact(raw) for raw in args.artifact]

        if args.publish and not args.build:
            run_publish(tag, output_dir(version), args.repo_slug)
            return 0

        with tempfile.TemporaryDirectory(prefix="ba-release-") as tmp:
            dest = output_dir(version) if mutating else Path(tmp)
            dest.mkdir(parents=True, exist_ok=True)
            archive = build_source_archive(ref, version, dest)
            for extra in extras:
                shutil.copy2(extra, dest / extra.name)
            artifacts = [archive, *[dest / e.name for e in extras]]
            manifest = write_manifest(dest, artifacts)
            notes = render_notes(version, tag, commit, artifacts, args.repo_slug)
            (dest / NOTES_NAME).write_text(notes, encoding="utf-8")

            print(f"release: source artifact {archive.name} ({archive.stat().st_size} bytes)")
            print(f"release: sha256 {sha256_file(archive)}")
            print(f"release: manifest {manifest.name} ({len(artifacts)} artifact(s))")

            if args.emit_formula:
                formula = emit_formula(version, dest, archive)
                print(f"release: wrote {formula.relative_to(REPO_ROOT)}")

            print(f"\n=== {NOTES_NAME} ===\n")
            print(notes)

            if mutating:
                print(f"release: artifacts written to {dest.relative_to(REPO_ROOT)}")
            else:
                print("release: DRY RUN — nothing was written outside a temporary directory")

            print_operator_commands(
                version,
                tag,
                output_dir(version),
                [p.name for p in release_assets(dest)],
                args.repo_slug,
            )

            if args.publish:
                run_publish(tag, dest, args.repo_slug)
        return 0
    except (ReleaseError, PolicyError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Release-policy primitives: version authority, semver validation, and the
`RELEASE.md` "Do Not Release" exclusion audit.

Kept separate from `scripts/release.py` so the rules are importable and
testable without going through the CLI, and so there is exactly one place
that encodes what may not ship in a source artifact.

Version authority
-----------------
The release version is owned by the annotated git tag `v<semver>`. The single
in-repo mirror of that value is `package.json` -> `version`; `release.py`
verifies the two agree and refuses to proceed on drift.

`desktop/_version.py` is deliberately NOT the authority: it is a tufup update
channel version, bumped automatically to a build timestamp by the desktop
artifact pipeline, so it cannot express a human-chosen release semver.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Official semver.org recommended pattern, anchored. Fail closed: anything
# that is not exactly a semver string is rejected (no "v" prefix, no
# whitespace, no partial "1.2", no leading zeroes).
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

MAX_VERSION_LENGTH = 64

VERSION_FILE = "package.json"

# Directory names that must never appear anywhere in a released source path.
DENY_PATH_SEGMENTS = frozenset(
    {
        "better-agent-private",
        "node_modules",
        "site-packages",
        "venv",
        ".venv",
        ".venvs",
        ".better-claude",
        ".better-agent",
        "__pycache__",
    }
)

# Built/downloadable distributables must be release ASSETS, never source
# archive members.
DENY_SUFFIXES = (
    ".dmg",
    ".pkg",
    ".apk",
    ".aab",
    ".exe",
    ".msi",
    ".appimage",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tar.xz",
)

# `.env` templates are documentation, not secrets; a real `.env` is a secret.
ALLOWED_ENV_BASENAMES = frozenset({".env.example", ".env.sample", ".env.template"})


class PolicyError(Exception):
    """Raised when a release-policy precondition is violated."""


def validate_version(raw: str) -> str:
    """Return `raw` if it is a strict semver string, else raise PolicyError."""
    if not isinstance(raw, str):
        raise PolicyError("version must be a string")
    if len(raw) > MAX_VERSION_LENGTH:
        raise PolicyError(f"version is too long (max {MAX_VERSION_LENGTH} chars)")
    if not SEMVER_RE.match(raw):
        raise PolicyError(
            f"invalid version {raw!r}: expected a bare semver string such as "
            "'0.2.0' or '0.2.0-rc.1' (no leading 'v', no whitespace)"
        )
    return raw


def tag_for_version(version: str) -> str:
    """The canonical git tag name for a validated version."""
    return f"v{validate_version(version)}"


def source_archive_name(version: str) -> str:
    return f"better-agent-{validate_version(version)}.tar.gz"


def archive_prefix(version: str) -> str:
    return f"better-agent-{validate_version(version)}/"


def version_file_path(repo_root: Path) -> Path:
    return repo_root / VERSION_FILE


def read_repo_version(repo_root: Path) -> str:
    """Read the in-repo mirror of the release version from package.json."""
    path = version_file_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"cannot read {VERSION_FILE}: {exc}") from exc
    except ValueError as exc:
        raise PolicyError(f"{VERSION_FILE} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{VERSION_FILE} must contain a JSON object")
    version = data.get("version")
    if not isinstance(version, str):
        raise PolicyError(f"{VERSION_FILE} has no string 'version' field")
    return version


def write_repo_version(repo_root: Path, version: str) -> None:
    """Rewrite only the `version` field of package.json, preserving key order.

    Writes the file and nothing else — never touches git.
    """
    validate_version(version)
    path = version_file_path(repo_root)
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    if not isinstance(data, dict):
        raise PolicyError(f"{VERSION_FILE} must contain a JSON object")
    data["version"] = version
    trailing = "\n" if original.endswith("\n") else ""
    path.write_text(json.dumps(data, indent=2) + trailing, encoding="utf-8")


def _basename_violation(basename: str) -> str | None:
    lowered = basename.lower()
    if lowered == ".env" or (lowered.startswith(".env.") and lowered not in ALLOWED_ENV_BASENAMES):
        return "environment file (may contain secrets)"
    for suffix in DENY_SUFFIXES:
        if lowered.endswith(suffix):
            return f"generated/downloadable artifact ({suffix})"
    return None


def audit_paths(paths: list[str]) -> list[str]:
    """Return human-readable violations for a list of archive-relative paths.

    Applied to the file list of the exact ref being released, so the audit
    reflects the artifact rather than the working tree.
    """
    violations: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            violations.append(f"{path}: unsafe path outside the archive root")
            continue
        segments = normalized.split("/")
        for segment in segments[:-1]:
            if segment.lower() in DENY_PATH_SEGMENTS:
                violations.append(f"{path}: excluded directory {segment!r}")
                break
        else:
            if segments[-1].lower() in DENY_PATH_SEGMENTS:
                violations.append(f"{path}: excluded name {segments[-1]!r}")
                continue
            reason = _basename_violation(segments[-1])
            if reason:
                violations.append(f"{path}: {reason}")
    return violations

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from paths import ba_home
from json_store import read_json, write_json

# `daemonhost` lives at the repo root, a sibling of `backend/`, not on
# sys.path by default. Entrypoints that import `extension_daemons` get it
# for free as a side effect of that module's own guard, but `main_node.py`
# (node-side handshake) never imports it, so this module needs its own
# guard wherever it reaches for `daemonhost` below.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


PUBLIC_ROLE = "app_public"
PRIVATE_ROLE = "config_private"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SCP_REMOTE = re.compile(r"^[^@]+@([^:]+):(.+)$")
_URL_REMOTE = re.compile(r"^[a-z]+://(?:[^@/]+@)?([^/]+)/(.+)$", re.I)


class RepositoryAlignmentError(RuntimeError):
    pass


def _attestation_path() -> Path:
    return ba_home() / "runtime" / "primary_repository_attestation.json"


def record_primary_attestation(accepted: bool) -> None:
    from daemonhost import pointer

    state = pointer.read()
    if state.get("status") != "switching":
        return
    transaction_id = str(state.get("request_id") or "")
    if transaction_id:
        write_json(
            _attestation_path(),
            {"transaction_id": transaction_id, "accepted": accepted},
        )


def primary_attestation_status(transaction_id: str) -> str:
    data = read_json(_attestation_path(), {})
    if not transaction_id or data.get("transaction_id") != transaction_id:
        return "pending"
    return "accepted" if data.get("accepted") is True else "rejected"


def finalize_node_activation(running_root: Path) -> str:
    from daemonhost import pointer

    state = pointer.read()
    if state.get("status") != "switching":
        return "active"
    transaction_id = str(state.get("request_id") or "")
    attestation = primary_attestation_status(transaction_id)
    if attestation == "accepted":
        pointer.confirm_healthy(str(running_root), transaction_id)
        return "active"
    if attestation == "rejected":
        pointer.revert("primary rejected repository generation", transaction_id)
    return attestation


def expire_node_activation(running_root: Path) -> bool:
    from daemonhost import pointer

    state = pointer.read()
    if (
        state.get("status") != "switching"
        or str(state.get("active") or "") != str(running_root.resolve())
    ):
        return False
    pointer.revert(
        "primary repository attestation did not arrive",
        str(state.get("request_id") or ""),
    )
    return True


def app_root() -> Path:
    return Path(__file__).resolve().parents[1]


def role_paths() -> dict[str, Path]:
    root = app_root()
    return {
        PUBLIC_ROLE: root,
        PRIVATE_ROLE: root / "better-agent-private",
    }


def _git(path: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _output(path: Path, *args: str) -> str:
    result = _git(path, *args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote(path: Path) -> str:
    names = [line for line in _output(path, "remote").splitlines() if line]
    if not names:
        return ""
    name = "origin" if "origin" in names else names[0]
    return _output(path, "remote", "get-url", name)


def _file_uri_path_text(remote: str, windows: bool = os.name == "nt") -> str:
    parsed = urlparse(remote)
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    elif windows and re.match(r"^/[a-z]:/", path, re.I):
        path = path[1:]
    return url2pathname(path)


def _local_remote_path(repository: Path, remote: str) -> Path | None:
    parsed = urlparse(remote)
    if parsed.scheme == "file":
        return Path(_file_uri_path_text(remote)).resolve()
    if parsed.scheme or _SCP_REMOTE.match(remote):
        return None
    candidate = Path(remote).expanduser()
    if not candidate.is_absolute():
        candidate = repository / candidate
    return candidate.resolve()


def _canonical_remote(path: Path, seen: set[Path] | None = None) -> str:
    repository = path.resolve()
    visited = set() if seen is None else seen
    if repository in visited:
        return _remote(repository)
    visited.add(repository)
    remote = _remote(repository)
    local = _local_remote_path(repository, remote)
    if local is None or not local.exists():
        return remote
    upstream = _canonical_remote(local, visited)
    return upstream or remote


def _dirty(path: Path) -> bool:
    result = _git(path, "status", "--porcelain", "--untracked-files=normal")
    return result.returncode != 0 or bool(result.stdout.strip())


def repository_identity(remote: str) -> str:
    text = remote.strip()
    match = _SCP_REMOTE.match(text) or _URL_REMOTE.match(text)
    if not match:
        return text.lower().rstrip("/")
    host = match.group(1).lower()
    path = match.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}"


def repository_snapshot(role: str, path: Path) -> dict[str, Any]:
    if not (path / ".git").exists():
        return {
            "role": role,
            "available": False,
            "commit_sha": "",
            "remote_url": "",
            "dirty": False,
        }
    return {
        "role": role,
        "available": True,
        "commit_sha": _output(path, "rev-parse", "HEAD").lower(),
        "remote_url": _canonical_remote(path),
        "dirty": _dirty(path),
        "source_kind": "git",
    }


def current_repositories(
    build_info: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    repositories = [
        repository_snapshot(role, path)
        for role, path in role_paths().items()
    ]
    public = repositories[0]
    packaged_sha = str((build_info or {}).get("commit_sha") or "").lower()
    if not public["available"] and _SHA.fullmatch(packaged_sha):
        public.update({
            "available": True,
            "commit_sha": packaged_sha,
            "source_kind": "packaged",
        })
    return repositories


def desired_manifest() -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for record in current_repositories():
        required = record["role"] == PUBLIC_ROLE or record["available"]
        if required and (
            not record["available"]
            or not _SHA.fullmatch(record["commit_sha"])
            or not record["remote_url"]
            or record["dirty"]
        ):
            raise RepositoryAlignmentError(
                f"{record['role']} is not a clean publishable repository"
            )
        manifest.append({**record, "required": required})
    return manifest


def repositories_match(observed: object, desired: list[dict[str, Any]]) -> bool:
    if not isinstance(observed, list):
        return False
    by_role = {
        row.get("role"): row
        for row in observed
        if isinstance(row, dict) and isinstance(row.get("role"), str)
    }
    for expected in desired:
        if not expected["required"]:
            continue
        actual = by_role.get(expected["role"])
        if not actual or actual.get("commit_sha") != expected["commit_sha"]:
            return False
        if actual.get("dirty") is True:
            return False
        if actual.get("source_kind") == "packaged":
            return False
        if (
            repository_identity(str(actual.get("remote_url") or ""))
            != repository_identity(expected["remote_url"])
        ):
            return False
    return True


def _validate_manifest(manifest: object) -> list[dict[str, str]]:
    if not isinstance(manifest, list):
        raise RepositoryAlignmentError("repository manifest must be a list")
    entries: list[dict[str, str]] = []
    for raw in manifest:
        if isinstance(raw, dict) and raw.get("required") is False:
            continue
        if not isinstance(raw, dict):
            raise RepositoryAlignmentError("repository manifest entry must be an object")
        role = raw.get("role")
        sha = str(raw.get("commit_sha") or "").lower()
        remote = str(raw.get("remote_url") or "").strip()
        if role not in role_paths() or not _SHA.fullmatch(sha) or not remote:
            raise RepositoryAlignmentError(f"invalid repository manifest for {role!r}")
        entries.append({"role": role, "commit_sha": sha, "remote_url": remote})
    if not any(entry["role"] == PUBLIC_ROLE for entry in entries):
        raise RepositoryAlignmentError("public repository is required")
    return entries


def _node_remote(entry: dict[str, str]) -> str:
    current = repository_snapshot(entry["role"], role_paths()[entry["role"]])
    if current["available"]:
        if current["dirty"]:
            raise RepositoryAlignmentError(f"{entry['role']} checkout is dirty")
        if repository_identity(current["remote_url"]) != repository_identity(
            entry["remote_url"]
        ):
            raise RepositoryAlignmentError(
                f"{entry['role']} repository identity mismatch"
            )
        return str(current["remote_url"])
    if entry["role"] == PUBLIC_ROLE:
        raise RepositoryAlignmentError(
            "packaged runtime cannot align to a source generation"
        )
    return entry["remote_url"]


def _clone_exact(remote: str, sha: str, target: Path, role: str) -> None:
    cloned = subprocess.run(
        ["git", "clone", "--no-checkout", remote, str(target)],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    if cloned.returncode != 0:
        raise RepositoryAlignmentError(
            f"{role} clone failed: {cloned.stderr.strip() or cloned.stdout.strip()}"
        )
    checked = _git(target, "checkout", "--detach", sha, timeout=90)
    if checked.returncode != 0:
        raise RepositoryAlignmentError(
            f"{role} checkout failed: {checked.stderr.strip() or checked.stdout.strip()}"
        )
    if _output(target, "rev-parse", "HEAD").lower() != sha or _dirty(target):
        raise RepositoryAlignmentError(f"{role} exact checkout attestation failed")


def _run(command: list[str], cwd: Path, timeout: float) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RepositoryAlignmentError(
            result.stderr.strip() or result.stdout.strip() or "generation preparation failed"
        )


def _prepare_runtime(public_root: Path) -> None:
    uv = shutil.which("uv")
    npm = shutil.which("npm")
    if not uv or not npm:
        raise RepositoryAlignmentError("source generation requires uv and npm")
    _run(
        [shutil.which("python") or os.sys.executable, "dependency_plan.py", "activate", "--uv", uv],
        public_root / "backend",
        600,
    )
    _run([npm, "ci"], public_root / "frontend", 600)
    _run([npm, "run", "build"], public_root / "frontend", 600)


def align_repositories(manifest: object) -> dict[str, Any]:
    entries = _validate_manifest(manifest)
    transaction_id = uuid.uuid4().hex
    generation = ba_home() / "runtime" / "generations" / transaction_id
    public_root = generation / "app"
    generation.mkdir(parents=True, exist_ok=False)
    try:
        public = next(entry for entry in entries if entry["role"] == PUBLIC_ROLE)
        _clone_exact(
            _node_remote(public),
            public["commit_sha"],
            public_root,
            PUBLIC_ROLE,
        )
        exclude = public_root / ".git" / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\nbetter-agent-private/\n")
        private = next(
            (entry for entry in entries if entry["role"] == PRIVATE_ROLE),
            None,
        )
        if private:
            _clone_exact(
                _node_remote(private),
                private["commit_sha"],
                public_root / "better-agent-private",
                PRIVATE_ROLE,
            )
        _prepare_runtime(public_root)
        from daemonhost import pointer

        pointer.confirm_healthy(str(app_root()))
        pointer.set_active(str(public_root), transaction_id)
    except BaseException:
        shutil.rmtree(generation, ignore_errors=True)
        raise
    return {
        "status": "staged",
        "transaction_id": transaction_id,
        "changed_roles": [entry["role"] for entry in entries],
        "repositories": current_repositories(),
        "restart_required": True,
    }

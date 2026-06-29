from __future__ import annotations

from typing import Optional

_SESSION_SIDECAR_SUFFIXES = (
    ".summary.json",
    ".drafts.json",
    ".seen.json",
    ".opened.json",
    ".fork-index.json",
    ".summary-index.json",
)


def sessions_fingerprint() -> tuple[int, int, int]:
    from paths import ba_home

    count = 0
    newest_mtime_ns = 0
    total_size = 0
    sessions_dir = ba_home() / "sessions"
    try:
        files = sessions_dir.glob("*.json")
    except OSError:
        return (0, 0, 0)
    for path in files:
        if path.name.endswith(_SESSION_SIDECAR_SUFFIXES):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
        total_size += stat.st_size
    return (count, newest_mtime_ns, total_size)


def rebuild_index(previous_fingerprint: tuple[int, int, int] | None = None) -> dict:
    fingerprint = sessions_fingerprint()
    if previous_fingerprint is not None and fingerprint == previous_fingerprint:
        return {"rebuilt": False, "fingerprint": fingerprint}
    from project_match import rebuild

    rebuild()
    return {"rebuilt": True, "fingerprint": fingerprint}


def suggest_project_payload(prompt: str, current_cwd: str) -> Optional[dict]:
    from project_match import suggest_project

    suggestion = suggest_project(prompt, current_cwd)
    if suggestion is None:
        return None
    return {
        "target_cwd": suggestion.target_cwd,
        "score": suggestion.score,
        "margin": suggestion.margin,
    }

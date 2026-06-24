from __future__ import annotations

from typing import Optional


def rebuild_index() -> bool:
    from project_match import rebuild

    rebuild()
    return True


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

"""Model-free tests for the project-match decision logic and loader."""
import json
import os
import tempfile
from pathlib import Path

from project_match.matcher import (
    decide, load_prompts_by_project, Suggestion, _softmax_entropy,
)


def _write_session(sessions, name, cwd, messages):
    (sessions / name).write_text(json.dumps({"id": name, "cwd": cwd, "messages": messages}))


def test_load_groups_prompts_by_cwd():
    with tempfile.TemporaryDirectory() as root:
        sessions = Path(root) / "sessions"
        sessions.mkdir()
        _write_session(sessions, "a.json", "/proj/a", [
            {"role": "user", "content": "deploy the backend"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": "scale workers"}]},
        ])
        _write_session(sessions, "b.json", "/proj/b", [
            {"role": "user", "content": "fix the css"},
            {"role": "user", "content": ""},  # empty -> skipped
        ])
        (sessions / "a.summary.json").write_text("{}")  # sidecar -> ignored
        by = load_prompts_by_project(sessions)
        assert by["/proj/a"] == ["deploy the backend", "scale workers"]
        assert by["/proj/b"] == ["fix the css"]


def test_decide_suggests_when_another_project_wins_by_margin():
    scores = {"/cur": 0.60, "/other": 0.80}
    s = decide(scores, "/cur", margin_threshold=0.05)
    assert isinstance(s, Suggestion) and s.target_cwd == "/other"
    assert abs(s.margin - 0.20) < 1e-9


def test_decide_silent_when_current_is_best():
    assert decide({"/cur": 0.9, "/other": 0.4}, "/cur", 0.05) is None


def test_decide_silent_when_margin_too_small():
    assert decide({"/cur": 0.70, "/other": 0.72}, "/cur", 0.05) is None


def test_decide_silent_when_current_unknown_or_single_project():
    assert decide({"/a": 0.9, "/b": 0.8}, "/unknown", 0.05) is None
    assert decide({"/a": 0.9}, "/a", 0.05) is None


# --- entropy guard tests ---

def test_entropy_uniform_scores_high():
    """All projects score identically -> entropy is 1.0 (maximum)."""
    scores = {"/a": 0.50, "/b": 0.50, "/c": 0.50, "/d": 0.50}
    assert _softmax_entropy(scores) > 0.99


def test_entropy_one_dominant_low():
    """One project scores much higher -> entropy well below threshold."""
    scores = {"/a": 0.40, "/b": 0.42, "/c": 0.41, "/winner": 0.70}
    e = _softmax_entropy(scores)
    assert e < 0.95


def test_decide_silent_on_generic_prompt():
    """Uniform scores (generic prompt) -> no suggestion even with margin."""
    scores = {"/cur": 0.48, "/other": 0.52, "/third": 0.50, "/fourth": 0.51}
    # margin is 0.04 < 0.05, so already blocked — but even with low threshold:
    assert decide(scores, "/cur", margin_threshold=0.01) is None


def test_decide_suggests_when_specific_prompt():
    """One project clearly dominant -> entropy low, suggestion passes."""
    scores = {"/cur": 0.50, "/other": 0.72, "/third": 0.49, "/fourth": 0.48}
    s = decide(scores, "/cur", margin_threshold=0.05)
    assert isinstance(s, Suggestion) and s.target_cwd == "/other"


def test_decide_silent_on_near_uniform_despite_margin():
    """Many projects with near-identical scores -> high entropy -> blocked."""
    scores = {f"/p{i}": 0.50 + 0.005 * i for i in range(10)}
    # /p9=0.545, /p0=0.500 -> margin 0.045 < 0.05, but test with lower threshold
    assert decide(scores, "/p0", margin_threshold=0.01) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")

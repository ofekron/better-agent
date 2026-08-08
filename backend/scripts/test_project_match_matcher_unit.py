"""Unit owner for project_match.matcher.

Covers the prompt-routing suggestion logic that sits on top of
project_match.embedding: text extraction, per-project prompt loading,
softmax-entropy discrimination, the margin-gated ``decide`` decision, the
``ProjectMatcher`` index lifecycle, and the module-level lazy/ready helpers.

The embedding collaborators (``embed``, ``EmbeddingTopic``,
``embedding_similarity``) are imported into matcher's namespace and are
monkeypatched here to deterministic fakes — this isolates the matcher unit.
Their own math is proven by test_project_match_embedding_unit.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import project_match.matcher as matcher  # noqa: E402
from project_match.matcher import (  # noqa: E402
    ENTROPY_THRESHOLD,
    MARGIN_THRESHOLD,
    ProjectMatcher,
    Suggestion,
    _softmax_entropy,
    _user_text,
    decide,
    load_prompts_by_project,
)


@pytest.fixture(autouse=True)
def _reset_matcher_global():
    """The module-level ``_matcher`` singleton leaks across tests; reset it."""
    saved = matcher._matcher
    matcher._matcher = None
    yield
    matcher._matcher = saved


# --- _user_text --------------------------------------------------------------

def test_user_text_passes_string_through():
    assert _user_text("hello") == "hello"


def test_user_text_joins_text_blocks_and_drops_non_text():
    content = [
        {"type": "text", "text": "alpha"},
        {"type": "image", "url": "x"},            # ignored: not text type
        {"not a dict": True},                      # ignored: wrong shape
        {"type": "text", "text": "beta"},
    ]
    assert _user_text(content) == "alpha beta"


def test_user_text_returns_empty_for_unknown_shape():
    assert _user_text(None) == ""
    assert _user_text(123) == ""


# --- load_prompts_by_project -------------------------------------------------

def _write_session(path: Path, **fields) -> None:
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_load_prompts_by_project_groups_user_text_by_cwd(tmp_path):
    _write_session(
        tmp_path / "a.json",
        cwd="/proj-a",
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "user", "content": [{"type": "text", "text": "world"}]},
            {"role": "assistant", "content": "ignored role"},
            {"role": "user", "content": "   "},        # whitespace-only, dropped
            {"role": "user", "content": None},         # unknown shape -> "" -> dropped
        ],
    )
    _write_session(tmp_path / "b.json", cwd="/proj-b",
                   messages=[{"role": "user", "content": "there"}])
    _write_session(tmp_path / "b.summary.json", cwd="/proj-b",
                   messages=[{"role": "user", "content": "summarized"}])  # skipped
    _write_session(tmp_path / "no-cwd.json",
                   messages=[{"role": "user", "content": "orphan"}])      # no cwd
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")  # parse error

    by_cwd = load_prompts_by_project(sessions_dir=tmp_path)

    assert by_cwd == {
        "/proj-a": ["hello", "world"],
        "/proj-b": ["there"],
    }


def test_load_prompts_by_project_default_path_uses_session_store(monkeypatch):
    # sessions_dir=None reads from session_store against the conftest test home
    # (empty -> no files). Patch the store to prove that branch is taken.
    import session_store

    monkeypatch.setattr(session_store, "_session_json_files", lambda: [])
    assert load_prompts_by_project() == {}


# --- _softmax_entropy --------------------------------------------------------

def test_softmax_entropy_is_zero_for_single_candidate():
    assert _softmax_entropy({"a": 0.9}) == 0.0


def test_softmax_entropy_is_one_for_uniform_scores():
    assert _softmax_entropy({"a": 1.0, "b": 1.0, "c": 1.0}) == pytest.approx(1.0)


def test_softmax_entropy_is_low_for_peaked_scores():
    peaked = _softmax_entropy({"a": 1.0, "b": 0.0})
    # Mass concentrated on one project -> well below the discrimination gate.
    assert 0.0 < peaked < ENTROPY_THRESHOLD


# --- decide ------------------------------------------------------------------

def test_decide_none_when_fewer_than_two_candidates():
    assert decide({"a": 0.9}, "a", MARGIN_THRESHOLD) is None


def test_decide_none_when_current_cwd_not_scored():
    assert decide({"a": 0.9, "b": 0.1}, "c", MARGIN_THRESHOLD) is None


def test_decide_none_when_current_is_already_best():
    assert decide({"a": 0.9, "b": 0.1}, "a", MARGIN_THRESHOLD) is None


def test_decide_none_when_margin_below_threshold():
    # entropy_threshold raised so entropy never blocks; isolate the margin gate.
    scores = {"a": 0.60, "b": 0.56, "c": 0.0}
    assert decide(scores, "b", margin_threshold=0.05, entropy_threshold=2.0) is None


def test_decide_none_when_scores_too_uniform_to_discriminate():
    scores = {"a": 0.5, "b": 0.5, "c": 0.5}
    # margin gate disabled (0.0); uniform entropy forces None.
    assert decide(scores, "b", margin_threshold=0.0) is None


def test_decide_returns_suggestion_on_clear_win():
    scores = {"a": 0.9, "b": 0.1, "c": 0.0}
    suggestion = decide(scores, "b", MARGIN_THRESHOLD)
    assert isinstance(suggestion, Suggestion)
    assert suggestion.target_cwd == "a"
    assert suggestion.score == 0.9
    assert suggestion.margin == pytest.approx(0.8)


# --- ProjectMatcher ----------------------------------------------------------

class _FakeTopic:
    """EmbeddingTopic stand-in carrying a label so similarity can vary per cwd."""

    def __init__(self, label: str):
        self.label = label

    @classmethod
    def from_prompts(cls, prompts):
        return cls(prompts[0])


def _patch_embedding_collaborators(monkeypatch, similarity_by_label):
    monkeypatch.setattr(matcher, "EmbeddingTopic", _FakeTopic)
    monkeypatch.setattr(matcher, "embed", lambda texts: np.array([[1.0, 0.0]]))
    monkeypatch.setattr(
        matcher, "embedding_similarity",
        lambda topic, v: similarity_by_label[topic.label],
    )


def test_matcher_build_keeps_only_projects_above_min_prompts(monkeypatch):
    monkeypatch.setattr(matcher, "EmbeddingTopic", _FakeTopic)
    m = ProjectMatcher(min_prompts=2).build({"/a": ["p1", "p2"], "/b": ["only"]})
    assert set(m._topics) == {"/a"}


def test_matcher_build_loads_prompts_when_none_provided():
    # Default arg -> real load_prompts_by_project() over the empty conftest home.
    m = ProjectMatcher().build()
    assert m._topics == {}


def test_matcher_scores_empty_when_index_unbuilt():
    assert ProjectMatcher().scores("anything") == {}


def test_matcher_scores_maps_each_topic_through_similarity(monkeypatch):
    _patch_embedding_collaborators(monkeypatch, {"/a": 0.9})
    m = ProjectMatcher(min_prompts=1).build({"/a": ["/a"]})
    assert m.scores("prompt") == {"/a": 0.9}


def test_matcher_suggest_delegates_to_decide(monkeypatch):
    _patch_embedding_collaborators(monkeypatch, {"/a": 0.9, "/b": 0.1})
    m = ProjectMatcher(min_prompts=1).build({"/a": ["/a"], "/b": ["/b"]})
    suggestion = m.suggest("prompt", current_cwd="/b")
    assert isinstance(suggestion, Suggestion)
    assert suggestion.target_cwd == "/a"


# --- module-level lifecycle --------------------------------------------------

def test_get_matcher_builds_lazily_and_caches():
    first = matcher.get_matcher()
    second = matcher.get_matcher()
    assert first is second
    assert matcher.is_ready() is True


def test_rebuild_creates_a_new_instance():
    old = matcher.get_matcher()
    new = matcher.rebuild()
    assert new is not old


def test_is_ready_false_before_first_build():
    assert matcher.is_ready() is False
    matcher.warm()
    assert matcher.is_ready() is True


def test_suggest_project_delegates_to_matcher():
    # Empty index (conftest home) -> decide sees <2 candidates -> None.
    assert matcher.suggest_project("prompt", "/cwd") is None


def test_suggest_if_ready_gates_on_index_state():
    # Unbuilt -> None without touching the index.
    assert matcher.suggest_if_ready("prompt", "/cwd") is None
    # Built but empty index -> still None, now via the delegation arm.
    matcher.get_matcher()
    assert matcher.suggest_if_ready("prompt", "/cwd") is None

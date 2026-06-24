"""Suggest the project a prompt most likely belongs to.

Used just before a prompt is sent, to catch the case where the user is about to
send into the wrong project. Each project is indexed by the embeddings of its
historical user prompts; an incoming prompt is scored against every project by
k-NN cosine. A switch is suggested only when another project beats the current
one by a confident margin (low false-positive rate) — otherwise nothing is
shown.
"""
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from paths import ba_home
from project_match.embedding import EmbeddingTopic, embed, embedding_similarity

MIN_PROMPTS = 20         # a project needs enough history to be a usable index
# How much better the target project must score than the current one before a
# switch is suggested. Tuned on real session data: 0.05 -> ~9% false-positive
# rate, catches ~33% of real mismatches, suggestion correct ~55% of the time.
# Lower it for more recall at the cost of more false alarms.
MARGIN_THRESHOLD = 0.05
# Entropy of softmax(scores) above this means the prompt is too generic to
# discriminate between projects (e.g. "continue this session"). 1.0 = perfectly
# uniform, 0.0 = all mass on one project.
ENTROPY_THRESHOLD = 0.97
_SOFTMAX_TEMPERATURE = 0.1


@dataclass
class Suggestion:
    target_cwd: str
    score: float   # similarity of the prompt to the suggested project, in [0, 1]
    margin: float  # score(target) - score(current)


def _user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def load_prompts_by_project(sessions_dir: Optional[Path] = None) -> dict[str, list[str]]:
    """User prompts grouped by project (the session's ``cwd``)."""
    sessions_dir = sessions_dir or (ba_home() / "sessions")
    by: dict[str, list[str]] = defaultdict(list)
    for f in sorted(sessions_dir.glob("*.json")):
        if f.name.endswith(".summary.json"):
            continue
        try:
            session = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        cwd = session.get("cwd")
        if not cwd:
            continue
        for msg in session.get("messages") or []:
            if msg.get("role") != "user":
                continue
            text = _user_text(msg.get("content")).strip()
            if text:
                by[cwd].append(text)
    return dict(by)


def _softmax_entropy(scores: dict[str, float], temperature: float = _SOFTMAX_TEMPERATURE) -> float:
    """Normalized Shannon entropy of softmax(scores / temperature).

    Returns value in [0, 1]: 1 = perfectly uniform (generic prompt), 0 = all
    mass on one project.  Softmax amplifies small raw-score differences into
    meaningful probability contrasts.
    """
    vals = list(scores.values())
    n = len(vals)
    if n < 2:
        return 0.0
    max_val = max(vals)
    exps = [math.exp((v - max_val) / temperature) for v in vals]
    total = sum(exps)
    probs = [e / total for e in exps]
    h = -sum(p * math.log(p) for p in probs if p > 0)
    h_max = math.log(n)
    return h / h_max if h_max > 0 else 0.0


def decide(
    scores: dict[str, float], current_cwd: str, margin_threshold: float,
    entropy_threshold: float = ENTROPY_THRESHOLD,
) -> Optional[Suggestion]:
    """Turn per-project similarity scores into a switch suggestion (or None).

    None when: fewer than two candidates, the current project is unknown, the
    current project already scores best, the lead is below the margin, or the
    score distribution is too uniform to discriminate (generic prompt).
    """
    if len(scores) < 2 or current_cwd not in scores:
        return None
    target = max(scores, key=scores.get)
    if target == current_cwd:
        return None
    margin = scores[target] - scores[current_cwd]
    if margin < margin_threshold:
        return None
    if _softmax_entropy(scores) > entropy_threshold:
        return None
    return Suggestion(target_cwd=target, score=scores[target], margin=margin)


class ProjectMatcher:
    def __init__(
        self, min_prompts: int = MIN_PROMPTS, margin_threshold: float = MARGIN_THRESHOLD,
        entropy_threshold: float = ENTROPY_THRESHOLD,
    ):
        self.min_prompts = min_prompts
        self.margin_threshold = margin_threshold
        self.entropy_threshold = entropy_threshold
        self._topics: dict[str, EmbeddingTopic] = {}

    def build(self, prompts_by_project: Optional[dict[str, list[str]]] = None) -> "ProjectMatcher":
        if prompts_by_project is None:
            prompts_by_project = load_prompts_by_project()
        self._topics = {
            cwd: EmbeddingTopic.from_prompts(prompts)
            for cwd, prompts in prompts_by_project.items()
            if len(prompts) >= self.min_prompts
        }
        return self

    def scores(self, prompt: str) -> dict[str, float]:
        if not self._topics:
            return {}
        v = embed([prompt])[0]
        return {cwd: embedding_similarity(t, v) for cwd, t in self._topics.items()}

    def suggest(self, prompt: str, current_cwd: str) -> Optional[Suggestion]:
        return decide(self.scores(prompt), current_cwd, self.margin_threshold,
                      self.entropy_threshold)


_matcher: Optional[ProjectMatcher] = None


def get_matcher() -> ProjectMatcher:
    """Process-wide matcher, built lazily on first use. Call ``rebuild`` to
    refresh the indexes after new prompts accumulate."""
    global _matcher
    if _matcher is None:
        _matcher = ProjectMatcher().build()
    return _matcher


def rebuild() -> ProjectMatcher:
    global _matcher
    _matcher = ProjectMatcher().build()
    return _matcher


def suggest_project(prompt: str, current_cwd: str) -> Optional[Suggestion]:
    """Convenience entry point: suggest a project to switch to, or None.

    Blocking: builds the index (model load ~34s) on first use. Use
    ``suggest_if_ready`` on request hot paths."""
    return get_matcher().suggest(prompt, current_cwd)


def is_ready() -> bool:
    """True once an index has been built (via ``warm``/``rebuild``)."""
    return _matcher is not None


def warm() -> ProjectMatcher:
    """Build the index if it isn't already. Idempotent; safe to call on a
    background thread at startup so the first request never pays the
    ~34s model-load + embed cost on the hot path."""
    return get_matcher()


def suggest_if_ready(prompt: str, current_cwd: str) -> Optional[Suggestion]:
    """Non-blocking suggest: returns None if the index isn't built yet.
    Never triggers a build — the ~34s model load stays off the request
    hot path; the startup warmer and the periodic rebuilder own builds."""
    if _matcher is None:
        return None
    return _matcher.suggest(prompt, current_cwd)

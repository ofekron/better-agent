"""Single source of truth for provider-runner error classification.

Per-provider ordered pattern tables (DATA) map stream/stderr text to a
classification {category, friendly message}. One shared `classify`
implementation walks the provider's rules first, then the common rules;
first match wins. `resume_session_mismatch` is the shared fail-closed
guard for resume-by-id runners whose stream reports the actual session id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from continuation import normalize_context_overflow_error

CATEGORY_AUTH = "auth"
CATEGORY_QUOTA_RATE_LIMIT = "quota_rate_limit"
CATEGORY_NETWORK = "network"
CATEGORY_SESSION_LOST = "session_lost"
CATEGORY_UNKNOWN = "unknown"


@dataclass(frozen=True)
class RunnerError:
    category: str
    message: str
    matched: str


_Rule = tuple[str, "re.Pattern[str]", Optional[str]]


def _rule(category: str, pattern: str, friendly: Optional[str] = None) -> _Rule:
    return (category, re.compile(pattern, re.IGNORECASE | re.DOTALL), friendly)


# Common rules appended after every provider table. Message defaults to the
# matched line when no friendly text is given.
_COMMON_RULES: tuple[_Rule, ...] = (
    _rule(CATEGORY_SESSION_LOST, r"no conversation found with session|session not found"),
    _rule(
        CATEGORY_AUTH,
        r"authentication failed|failed to authenticate|not authenticated"
        r"|unauthorized|invalid api key|oauth token has expired"
        r"|api key.{0,40}(?:invalid|expired|not configured|not set)"
        r"|\bhttp 401\b|\b401 unauthorized\b",
    ),
    _rule(
        CATEGORY_QUOTA_RATE_LIMIT,
        r"quota exceeded|rate.?limit|too many requests|\bhttp 429\b|\b429\b.{0,40}too many"
        r"|insufficient credit|out of credits|payment required|capacity exceeded",
    ),
    _rule(
        CATEGORY_NETWORK,
        r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|ENOTFOUND|EAI_AGAIN|EAI_NONAME"
        r"|getaddrinfo|could not resolve|socket hang up|network error"
        r"|TLS handshake|SSL handshake|HTTP 50[23]|bad gateway"
        r"|service unavailable|temporarily unavailable|overloaded",
    ),
)

# Provider-specific rules (ordered, checked before the common rules).
_PROVIDER_RULES: dict[str, tuple[_Rule, ...]] = {
    "pi": (
        _rule(
            CATEGORY_AUTH,
            r"no api key found|(?=.*/login)(?=.*provider)",
            "pi CLI has no credentials for the selected model's provider. "
            "Run `pi` interactively and use /login, or export the provider's "
            "API key env var (e.g. ANTHROPIC_API_KEY), then retry.",
        ),
    ),
    "qwen": (
        _rule(
            CATEGORY_SESSION_LOST,
            r"no saved session found",
            "Qwen session not found — the saved session for this conversation "
            "no longer exists. Start a fresh session.",
        ),
        _rule(
            CATEGORY_AUTH,
            r"no auth type is selected",
            "Qwen CLI has no auth configured. Complete the qwen OAuth login "
            "or set OPENAI_API_KEY, then retry.",
        ),
    ),
    "cursor": (
        _rule(
            CATEGORY_AUTH,
            r"authentication required|not authenticated|cursor-agent login",
            "Cursor CLI is not authenticated. Run `cursor-agent login` "
            "(or set CURSOR_API_KEY) and retry.",
        ),
    ),
    "kimi": (),
    "amp": (
        _rule(
            CATEGORY_AUTH,
            r"api key is not configured|(?=.*amp login)(?=.*api key)",
            "Amp CLI is not authenticated. Run `amp login` or set "
            "AMP_API_KEY, then retry.",
        ),
        _rule(CATEGORY_QUOTA_RATE_LIMIT, r"insufficient credit|out of free credits"),
    ),
    "opencode": (),
}


def _rules_for(kind: str) -> tuple[_Rule, ...]:
    if kind not in _PROVIDER_RULES:
        raise ValueError(f"unknown runner kind for error classification: {kind!r}")
    return _PROVIDER_RULES[kind] + _COMMON_RULES


def _matched_line(corpus: str, pattern: "re.Pattern[str]") -> Optional[str]:
    for raw_line in corpus.splitlines():
        line = raw_line.strip()
        if line and pattern.search(line):
            return line
    return None


def classify(kind: str, *texts: Optional[str]) -> Optional[RunnerError]:
    """Classify provider output/stderr text. First matching rule wins;
    returns None when nothing matches (caller decides the fallback)."""
    corpus = "\n".join(t for t in texts if t)
    if not corpus.strip():
        return None
    for category, pattern, friendly in _rules_for(kind):
        match = pattern.search(corpus)
        if not match:
            continue
        matched = _matched_line(corpus, pattern) or match.group(0).strip() or corpus.strip()[:200]
        return RunnerError(category=category, message=friendly or matched, matched=matched)
    return None


_STACK_FRAME_RE = re.compile(r"^\s+at\s+")
_NAMED_ERROR_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*Error:")


def extract_stderr_error(stderr_text: str) -> Optional[str]:
    """Best-effort single-line error extraction from unclassified stderr:
    first a named `SomethingError:` line, then an `error:`-prefixed line,
    then the last meaningful (non stack-frame, non bracket) line."""
    lines = stderr_text.splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if line and _NAMED_ERROR_RE.search(line):
            return normalize_context_overflow_error(line) or line
    for raw_line in lines:
        line = raw_line.strip()
        if line.lower().startswith("error:"):
            return normalize_context_overflow_error(line) or line
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line or _STACK_FRAME_RE.match(line) or line in {"}", "]", "["}:
            continue
        return normalize_context_overflow_error(line) or line
    return None


def stderr_error(kind: str, stderr_text: str) -> Optional[str]:
    """Classified friendly message when a rule matches, else the extracted
    raw error line, else None."""
    hit = classify(kind, stderr_text)
    if hit:
        return hit.message
    return extract_stderr_error(stderr_text)


def resume_session_mismatch(
    kind: str,
    requested_session_id: Optional[str],
    observed_session_id: Optional[str],
) -> Optional[RunnerError]:
    """Fail-closed session-loss guard: a resume was requested with id X but
    the provider stream reported a different id — the provider silently
    started a new session instead of resuming."""
    requested = str(requested_session_id or "").strip()
    observed = str(observed_session_id or "").strip()
    if not requested or not observed or requested == observed:
        return None
    return RunnerError(
        category=CATEGORY_SESSION_LOST,
        message=(
            f"{kind} session lost: resume requested session {requested} but the "
            f"provider stream reported session {observed}. Failing instead of "
            f"silently continuing on a fresh session."
        ),
        matched=observed,
    )

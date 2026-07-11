import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from dateutil import parser as date_parser


RATE_LIMIT_KEYWORDS = (
    "limit reached",
    "rate limit",
    "usage limit",
    "quota exceeded",
    "quota has been exhausted",
    "out of free quota",
    "resource exhausted",
    "resource_exhausted",
    "exhausted your capacity",
    "too many requests",
    "status: 429",
    "error 429",
    "http 429",
    "subscription window",
    "no more messages",
    "hit your limit",
    "hit the limit",
    "reached your usage limit",
)

_CLAUDE_FULL_RESET_RE = re.compile(
    r"resets\s+(\w+)\s+(\d{1,2})\s+at\s+(\d{1,2})(am|pm)", re.IGNORECASE,
)
_CLAUDE_SHORT_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(am|pm)", re.IGNORECASE,
)
_CODEX_RETRY_RE = re.compile(r"try again at\s+([^\n.]+)", re.IGNORECASE)
_ORDINAL_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
_GEMINI_DURATION_RE = re.compile(
    r"reset after\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", re.IGNORECASE,
)


def build_corpus(
    error: Optional[str],
    events: list[dict],
    extract_event_text: Callable[[list[dict]], str],
) -> str:
    parts: list[str] = []
    if isinstance(error, str) and error:
        parts.append(error[-2000:])
    event_text = extract_event_text(events)
    if event_text:
        parts.append(event_text[-2000:])
    return "\n".join(parts)


def is_rate_limit_text(value: object, extra_keywords: tuple[str, ...] = ()) -> bool:
    if not isinstance(value, str):
        return False
    corpus = value[-2000:].casefold()
    return any(keyword in corpus for keyword in RATE_LIMIT_KEYWORDS + extra_keywords)


def parse_rate_limit(
    provider_kind: str,
    corpus: str,
    *,
    extra_keywords: tuple[str, ...] = (),
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    if not is_rate_limit_text(corpus, extra_keywords):
        return None
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    kind = provider_kind.casefold()
    if kind == "claude":
        return _parse_claude(corpus, current)
    if kind == "codex":
        return _parse_codex(corpus, current)
    if kind in ("gemini", "qwen"):
        return _parse_gemini(corpus, current)
    if kind == "openai":
        if any(
            keyword in corpus.casefold()
            for keyword in ("subscription window", "quota exceeded", "usage limit")
        ):
            return current + timedelta(hours=1)
        return current + timedelta(minutes=1)
    return current + timedelta(hours=1)


def _parse_claude(corpus: str, now: datetime) -> datetime:
    match = _CLAUDE_FULL_RESET_RE.search(corpus)
    if match:
        month_text, day_text, hour_text, meridiem = match.groups()
        try:
            parsed = date_parser.parse(
                f"{month_text} {day_text} {now.year} {hour_text}{meridiem}",
                fuzzy=True,
            ).replace(tzinfo=now.tzinfo)
            if parsed <= now:
                parsed = parsed.replace(year=parsed.year + 1)
            return parsed
        except (ValueError, OverflowError):
            pass
    match = _CLAUDE_SHORT_RESET_RE.search(corpus)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(2).casefold() == "pm":
            hour += 12
        parsed = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if parsed <= now:
            parsed += timedelta(days=1)
        return parsed
    return now + timedelta(hours=1)


def _parse_codex(corpus: str, now: datetime) -> datetime:
    match = _CODEX_RETRY_RE.search(corpus)
    if not match:
        return now + timedelta(hours=1)
    text = _ORDINAL_RE.sub("", match.group(1))
    try:
        parsed = date_parser.parse(text, fuzzy=True, default=now.replace(tzinfo=None))
    except (ValueError, OverflowError):
        return now + timedelta(hours=1)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    if parsed <= now:
        parsed += timedelta(days=1)
    return parsed


def _parse_gemini(corpus: str, now: datetime) -> datetime:
    match = _GEMINI_DURATION_RE.search(corpus)
    if match and any(group is not None for group in match.groups()):
        hours, minutes, seconds = (int(group or 0) for group in match.groups())
        return now + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if "daily quota" in corpus.casefold():
        tomorrow = now.date() + timedelta(days=1)
        return datetime.combine(tomorrow, datetime.min.time(), tzinfo=now.tzinfo)
    return now + timedelta(hours=1)

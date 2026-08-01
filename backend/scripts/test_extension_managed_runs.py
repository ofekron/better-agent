from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_managed_runs as emr  # noqa: E402

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# _text_from_events — pure parser. The run() execution body (SubprocessAgent +
# coordinator wiring) belongs in the full-stack tier; here we cover the parser
# exhaustively plus run()'s input-validation branches that need no subprocess.
# ---------------------------------------------------------------------------


def _assistant(text, *, content=None):
    if content is None:
        content = text
    return {"type": "agent_message", "data": {"type": "assistant", "message": {"content": content}}}


def test_empty_events_returns_empty_string():
    assert emr._text_from_events([]) == ""


def test_non_dict_events_are_skipped():
    assert emr._text_from_events([None, 42, "str", []]) == ""


def test_non_agent_message_events_are_skipped():
    events = [
        {"type": "tool_result", "data": {"type": "assistant", "message": {"content": "x"}}},
        {"type": "agent_message", "data": {"type": "tool", "message": {"content": "y"}}},
    ]
    assert emr._text_from_events(events) == ""


def test_missing_or_falsy_data_is_treated_as_empty():
    # data = event.get("data") or {} -> None / falsy coerces to {}
    events = [
        {"type": "agent_message"},  # no data key
        {"type": "agent_message", "data": None},
        {"type": "agent_message", "data": 0},
    ]
    assert emr._text_from_events(events) == ""


def test_non_dict_message_is_skipped():
    events = [{"type": "agent_message", "data": {"type": "assistant", "message": "not-a-dict"}}]
    assert emr._text_from_events(events) == ""


def test_missing_content_is_skipped():
    events = [{"type": "agent_message", "data": {"type": "assistant", "message": {}}}]
    assert emr._text_from_events(events) == ""


def test_string_content_is_appended():
    assert emr._text_from_events([_assistant("hello")]) == "hello"


def test_empty_string_content_is_skipped():
    # isinstance(content, str) and content -> falsy content not appended,
    # and content is not a list either, so nothing is added.
    assert emr._text_from_events([_assistant("", content="")]) == ""


def test_multiple_string_parts_joined_and_stripped():
    events = [_assistant("foo"), _assistant("bar")]
    assert emr._text_from_events(events) == "foo bar"


def test_list_content_extracts_text_blocks():
    content = [
        {"type": "text", "text": "alpha"},
        {"type": "text", "text": "beta"},
    ]
    assert emr._text_from_events([_assistant("", content=content)]) == "alpha beta"


def test_list_content_skips_non_text_blocks():
    content = [
        {"type": "tool_use", "text": "ignored"},  # wrong type
        {"type": "text", "text": "kept"},
    ]
    assert emr._text_from_events([_assistant("", content=content)]) == "kept"


def test_list_content_skips_malformed_blocks():
    content = [
        "not-a-dict",                      # not a dict
        {"type": "text"},                  # no text key
        {"type": "text", "text": 123},     # text not str
        {"type": "text", "text": ""},      # empty text
        {"type": "text", "text": "valid"}, # valid
    ]
    assert emr._text_from_events([_assistant("", content=content)]) == "valid"


def test_mixed_string_and_list_content_across_events():
    events = [
        _assistant("intro"),
        _assistant("", content=[{"type": "text", "text": "body"}]),
        {"type": "agent_message", "data": {"type": "assistant", "message": {"content": "   "}}},
    ]
    result = emr._text_from_events(events)
    assert result == "intro body"


def test_event_prefix_regex_accepts_and_rejects():
    assert emr._EVENT_PREFIX_RE.fullmatch("managed_run")
    assert emr._EVENT_PREFIX_RE.fullmatch("a")
    assert emr._EVENT_PREFIX_RE.fullmatch("a1_b2")
    assert emr._EVENT_PREFIX_RE.fullmatch("z" + "_" * 39)  # 40 chars total (max: first + 39)
    # rejects
    assert emr._EVENT_PREFIX_RE.fullmatch("") is None
    assert emr._EVENT_PREFIX_RE.fullmatch("1abc") is None  # digit first
    assert emr._EVENT_PREFIX_RE.fullmatch("Bad") is None    # uppercase
    assert emr._EVENT_PREFIX_RE.fullmatch("a-b") is None    # dash not allowed
    assert emr._EVENT_PREFIX_RE.fullmatch("a" * 42) is None  # too long


# ---------------------------------------------------------------------------
# run() input-validation branches (raise before any SubprocessAgent work).
# The coordinator is never reached for these failing inputs, so None suffices.
# ---------------------------------------------------------------------------


def _run_kwargs(**overrides):
    base = dict(
        managed_session_id="managed-1",
        parent_session_id="",
        prompt="do something",
        model="m",
        cwd="/tmp",
    )
    base.update(overrides)
    return base


async def test_run_rejects_invalid_event_prefix():
    with pytest.raises(ValueError, match="event_prefix"):
        await emr.run(None, **_run_kwargs(event_prefix="Bad Prefix!"))


async def test_run_rejects_empty_prompt():
    with pytest.raises(ValueError, match="prompt is required"):
        await emr.run(None, **_run_kwargs(prompt=""))


async def test_run_rejects_unknown_managed_session(monkeypatch):
    monkeypatch.setattr(emr.session_manager, "get_lite", lambda _sid: None)
    with pytest.raises(ValueError, match="managed_session_id does not exist"):
        await emr.run(None, **_run_kwargs())


async def test_run_rejects_unknown_parent_session(monkeypatch):
    def fake_get_lite(sid):
        # managed exists, parent does not
        return {"cwd": "/tmp"} if sid == "managed-1" else None

    monkeypatch.setattr(emr.session_manager, "get_lite", fake_get_lite)
    with pytest.raises(ValueError, match="parent_session_id does not exist"):
        await emr.run(None, **_run_kwargs(parent_session_id="parent-ghost"))

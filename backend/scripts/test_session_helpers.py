from __future__ import annotations

import asyncio
import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-session-helpers-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import session_helpers  # noqa: E402


class FakeManager:
    """Stand-in for session_manager.manager: records calls and serves
    controlled get_lite/exists answers so every wrapper branch is
    deterministic without touching real session state."""

    def __init__(self, lite_map: dict, exists_set: set) -> None:
        self._lite_map = lite_map
        self._exists_set = exists_set
        self.get_lite_calls: list[str] = []
        self.exists_calls: list[str] = []

    def get_lite(self, session_id: str):
        self.get_lite_calls.append(session_id)
        return self._lite_map.get(session_id)

    def exists(self, session_id: str) -> bool:
        self.exists_calls.append(session_id)
        return session_id in self._exists_set


@pytest.fixture
def fake_manager(monkeypatch):
    fake = FakeManager(
        lite_map={"s1": {"id": "s1"}, "s2": {"id": "s2", "x": 1}},
        exists_set={"s1", "s2"},
    )
    monkeypatch.setattr(session_helpers, "session_manager", fake)
    return fake


def test_require_session_returns_snapshot(fake_manager):
    assert session_helpers.require_session("s2") == {"id": "s2", "x": 1}
    assert fake_manager.get_lite_calls == ["s2"]


def test_require_session_raises_404_when_missing(fake_manager):
    with pytest.raises(HTTPException) as exc:
        session_helpers.require_session("nope")
    assert exc.value.status_code == 404
    assert fake_manager.get_lite_calls == ["nope"]


def test_session_exists_true_and_false(fake_manager):
    assert asyncio.run(session_helpers.session_exists("s1")) is True
    assert asyncio.run(session_helpers.session_exists("nope")) is False
    assert fake_manager.exists_calls == ["s1", "nope"]


def test_session_lite_returns_dict_or_none(fake_manager):
    assert asyncio.run(session_helpers.session_lite("s1")) == {"id": "s1"}
    assert asyncio.run(session_helpers.session_lite("missing")) is None
    assert fake_manager.get_lite_calls == ["s1", "missing"]


def test_existing_session_ids_filters(fake_manager):
    assert session_helpers.existing_session_ids(["s1", "ghost", "s2"]) == {"s1", "s2"}
    assert fake_manager.exists_calls == ["s1", "ghost", "s2"]


def test_session_lite_by_id_includes_none(fake_manager):
    result = session_helpers.session_lite_by_id(["s1", "ghost"])
    assert result == {"s1": {"id": "s1"}, "ghost": None}
    assert fake_manager.get_lite_calls == ["s1", "ghost"]


def test_existing_session_ids_async(fake_manager):
    # Pass a generator to prove the set() coercion inside the wrapper runs;
    # set() reorders iteration, so compare calls as a sorted multiset.
    ids = (sid for sid in ["s2", "ghost", "s1"])
    assert asyncio.run(session_helpers.existing_session_ids_async(ids)) == {"s1", "s2"}
    assert sorted(fake_manager.exists_calls) == ["ghost", "s1", "s2"]


def test_session_lite_by_id_async(fake_manager):
    ids = (sid for sid in ["s1", "ghost", "s2"])
    result = asyncio.run(session_helpers.session_lite_by_id_async(ids))
    assert result == {
        "s1": {"id": "s1"},
        "ghost": None,
        "s2": {"id": "s2", "x": 1},
    }
    assert sorted(fake_manager.get_lite_calls) == ["ghost", "s1", "s2"]


def test_require_session_async_returns_snapshot(fake_manager):
    assert asyncio.run(session_helpers.require_session_async("s1")) == {"id": "s1"}


def test_require_session_async_raises_404_when_missing(fake_manager):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(session_helpers.require_session_async("missing"))
    assert exc.value.status_code == 404

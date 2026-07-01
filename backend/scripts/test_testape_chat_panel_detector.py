#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_test_home.isolate("bc-test-testape-chat-panel-")

import pytest  # noqa: E402

import testape_chat_panel_detector as det  # noqa: E402


def session_tree():
    return {
        "id": "root",
        "kind": "user",
        "messages": [
            {"id": "u1", "role": "user", "seq": 1},
            {"id": "a1", "role": "assistant", "seq": 2},
            {"id": "u2", "role": "user", "seq": 3},
            {"id": "a2", "role": "assistant", "seq": 4},
        ],
        "forks": [
            {
                "id": "fork",
                "kind": "user",
                "fork_point_seq": 2,
                "messages": [
                    {"id": "u1", "role": "user", "seq": 1},
                    {"id": "a1", "role": "assistant", "seq": 2},
                    {"id": "uf", "role": "user", "seq": 3},
                    {"id": "af", "role": "assistant", "seq": 4},
                ],
                "forks": [],
            }
        ],
    }


def payload(regions):
    return {
        "session_id": "root",
        "tree": {"visible": True, "session_id": "root", "regions": regions},
        "session": session_tree(),
    }


def test_compare_accepts_visible_linear_subsequence():
    assert det.compare_chat_panel_payload(
        payload(
            [
                {
                    "kind": "linear",
                    "session_id": "root",
                    "messages": [
                        {"id": "u1", "role": "user"},
                        {"id": "u2", "role": "user"},
                    ],
                }
            ]
        )
    ) == []


def test_compare_fails_unexpected_message():
    mismatches = det.compare_chat_panel_payload(
        payload(
            [
                {
                    "kind": "linear",
                    "session_id": "root",
                    "messages": [{"id": "ghost", "role": "assistant"}],
                }
            ]
        )
    )

    assert mismatches == ["region 0 rendered unexpected message ghost"]


def test_compare_fails_wrong_role_and_order():
    mismatches = det.compare_chat_panel_payload(
        payload(
            [
                {
                    "kind": "linear",
                    "session_id": "root",
                    "messages": [
                        {"id": "u2", "role": "assistant"},
                        {"id": "u1", "role": "user"},
                    ],
                }
            ]
        )
    )

    assert "region 0 message u2 role 'assistant' != 'user'" in mismatches
    assert "region 0 message u1 is out of session order" in mismatches


def test_compare_validates_fork_regions_against_split_projection():
    assert det.compare_chat_panel_payload(
        payload(
            [
                {
                    "kind": "fork_shared",
                    "session_id": "root",
                    "messages": [
                        {"id": "u1", "role": "user"},
                        {"id": "a1", "role": "assistant"},
                    ],
                },
                {
                    "kind": "fork_pane",
                    "session_id": "fork",
                    "messages": [
                        {"id": "uf", "role": "user"},
                        {"id": "af", "role": "assistant"},
                    ],
                },
            ]
        )
    ) == []


def test_compare_fails_when_extractor_missing():
    assert det.compare_chat_panel_payload({"session": session_tree()}) == [
        "Better Agent TestApe chat panel extractor is not installed"
    ]


def test_validate_uses_testape_eval_payload():
    original_session = det._web_session

    class Web:
        def navigate(self, url):
            assert url == "http://localhost:8000/s/root"

        def eval_js(self, script):
            assert "extractVisibleChatPanelTree" in script
            return payload(
                [
                    {
                        "kind": "linear",
                        "session_id": "root",
                        "messages": [{"id": "u1", "role": "user"}],
                    }
                ]
            )

    @contextmanager
    def fake_session(_adapter_id, _fs_url):
        yield Web()

    det._web_session = fake_session
    try:
        result = det.validate_chat_panel(
            adapter_id="web",
            session_id="root",
            url="http://localhost:8000/s/root",
        )
    finally:
        det._web_session = original_session

    assert result.ok is True
    assert result.adapter_id == "web"
    assert result.session_id == "root"


def test_loopback_guard_rejects_external_url():
    with pytest.raises(ValueError):
        det.validate_chat_panel(adapter_id="web", url="https://example.com/s/root")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))

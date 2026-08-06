"""Unit tests for the `permissions.ai_rank_kinds` manifest surface added to
`backend/extension_store.py`: validation shape, the `{kind: task_key}`
accessor used by the `/api/internal/ai-rank` route, and its contribution to
the settings-facing internal-LLM task-key listings (without also widening
`extension_provisioned_internal_llm_tasks`, the inline_spec gate).
"""
from __future__ import annotations

import json
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ai-rank-manifest-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import extension_store  # noqa: E402


def _record(permissions: dict) -> dict:
    return {"manifest": {"id": "test.ai-rank-manifest-ext", "permissions": permissions}}


def test_validate_permissions_accepts_ai_rank_kinds_dict():
    validated = extension_store._validate_permissions({
        "spawn_runs": True,
        "ai_rank_kinds": {"cards": "agent_board_card_search"},
    })
    assert validated["ai_rank_kinds"] == {"cards": "agent_board_card_search"}


@pytest.mark.parametrize(
    "ai_rank_kinds",
    [
        "not-a-dict",
        ["cards"],
        {"Cards": "agent_board_card_search"},  # kind must be lowercase snake_case
        {"cards!": "agent_board_card_search"},
        {"cards": "Agent Board Card Search"},  # task_key must match the same pattern
        {"cards": 5},
        {5: "agent_board_card_search"},
    ],
)
def test_validate_permissions_rejects_malformed_ai_rank_kinds(ai_rank_kinds):
    with pytest.raises(extension_store.ExtensionError):
        extension_store._validate_permissions({"ai_rank_kinds": ai_rank_kinds})


def test_extension_ai_rank_kinds_reads_the_declaration():
    record = _record({"ai_rank_kinds": {"cards": "agent_board_card_search"}})
    assert extension_store.extension_ai_rank_kinds(record) == {"cards": "agent_board_card_search"}


def test_extension_ai_rank_kinds_empty_when_undeclared():
    assert extension_store.extension_ai_rank_kinds(_record({})) == {}


def test_ai_rank_task_keys_appear_in_settings_facing_listings():
    record = _record({"ai_rank_kinds": {"cards": "agent_board_card_search"}})
    assert "agent_board_card_search" in extension_store.extension_internal_llm_tasks(record)


def test_ai_rank_task_keys_do_not_widen_inline_spec_allowance():
    """Declaring ai_rank_kinds must NOT also grant arbitrary-prompt
    inline_spec dispatch under the same task_key (least privilege) --
    `extension_provisioned_internal_llm_tasks` stays scoped to explicit
    `internal_llm_tasks` declarations only."""
    record = _record({"ai_rank_kinds": {"cards": "agent_board_card_search"}})
    assert "agent_board_card_search" not in extension_store.extension_provisioned_internal_llm_tasks(record)


def test_ai_rank_task_keys_contribute_to_extension_internal_llm_task_keys():
    data = extension_store._load()  # type: ignore[attr-defined]
    snapshot = json.loads(json.dumps(data))
    try:
        data.setdefault("extensions", {})["test.ai-rank-task-keys-ext"] = {
            "manifest": {
                "id": "test.ai-rank-task-keys-ext",
                "permissions": {"ai_rank_kinds": {"widgets": "widget_rank_task"}},
            },
            "enabled": True,
            "source": {"type": "git", "install_path": ""},
            "entitlement": {"status": "not_required"},
        }
        extension_store._save(data)  # type: ignore[attr-defined]
        assert "widget_rank_task" in extension_store.extension_internal_llm_task_keys()
        assert "widget_rank_task" in extension_store.all_internal_llm_task_keys()
    finally:
        extension_store._save(snapshot)  # type: ignore[attr-defined]

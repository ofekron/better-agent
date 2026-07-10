from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_switch_state_capability_returns_owner_envelope(monkeypatch) -> None:
    import capability_api
    from daemonhost import switch_control
    from global_events import AUTHORITY_EPOCH, authority_metadata

    monkeypatch.setenv("BETTER_AGENT_ACTIVE_CHECKOUT", "/checkout")
    monkeypatch.setattr(switch_control, "state", lambda _checkout: {"active": "dev"})
    action = capability_api._ACTIONS[("switch-control", "state.get")]
    result = action.handler(action.schema())
    assert result == {
        **authority_metadata("switch_control"),
        "data": {"active": "dev"},
    }
    assert result["authority_epoch"] == AUTHORITY_EPOCH


def test_machine_pending_returns_owner_envelope(monkeypatch) -> None:
    import main
    import node_link
    from global_events import AUTHORITY_EPOCH, authority_metadata

    monkeypatch.setattr(main, "_require_machine_nodes_internal", lambda _token: None)
    monkeypatch.setattr(node_link, "public_pending_nodes_cached", lambda: [{"node_id": "n1"}])
    result = asyncio.run(main.internal_list_pending_nodes({}, "token"))
    assert result == {
        **authority_metadata("machine_nodes"),
        "data": {"pending_nodes": [{"node_id": "n1"}]},
    }
    assert result["authority_epoch"] == AUTHORITY_EPOCH

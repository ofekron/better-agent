from __future__ import annotations

import os
from contextlib import contextmanager


@contextmanager
def scoped_turn_test_runtime():
    import installation_profile
    import session_manager as session_manager_module
    import session_queue_projection
    from session_manager import manager as session_manager

    original_auth_bypass = os.environ.get("BETTER_CLAUDE_TEST_AUTH_BYPASS")
    original_debounce = session_manager_module.PERSIST_DEBOUNCE_S
    original_integrations = installation_profile.integrations_enabled
    original_allowed = installation_profile.assert_orchestration_mode_allowed
    original_projection = session_queue_projection.note_persisted_tree
    original_flush = session_manager.flush_root_persist
    try:
        os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
        session_manager_module.PERSIST_DEBOUNCE_S = 0.0
        installation_profile.integrations_enabled = lambda: True
        installation_profile.assert_orchestration_mode_allowed = lambda _mode: None
        session_queue_projection.note_persisted_tree = lambda *_args, **_kwargs: 0
        session_manager.flush_root_persist = lambda _root_id: None
        yield
    finally:
        session_manager_module.PERSIST_DEBOUNCE_S = original_debounce
        installation_profile.integrations_enabled = original_integrations
        installation_profile.assert_orchestration_mode_allowed = original_allowed
        session_queue_projection.note_persisted_tree = original_projection
        session_manager.flush_root_persist = original_flush
        if original_auth_bypass is None:
            os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)
        else:
            os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = original_auth_bypass

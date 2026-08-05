"""Regression: turn_start frames must name their Better Agent session."""

from __future__ import annotations

import os
import re


def test_turn_start_emits_app_session_id():
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(backend, "turn_manager.py"), encoding="utf-8").read()
    matches = re.findall(
        r'"type":\s*"turn_start"\s*,\s*"data":\s*\{([^}]*)',
        src,
    )
    assert matches, "no turn_start emit found"
    assert all('"app_session_id": app_session_id' in body for body in matches)

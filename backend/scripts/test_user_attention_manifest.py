"""Validates the shipped user-attention extension manifest: empty
permissions, no surface outside _ALLOWED_SURFACES, no runtime_mcp, and both
applied_config tag rules (needs-decision orange + all-tasks-done blue dot)
parsed correctly.

Run with:
    cd backend && .venv/bin/python scripts/test_user_attention_manifest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-user-attention-manifest-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402

_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "extensions" / "user-attention" / "better-agent-extension.json"
)


def main() -> int:
    try:
        raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        m = extension_store.validate_manifest(raw)

        assert m["id"] == "ofek-dev.user-attention", m["id"]
        assert m["permissions"] == {}, m["permissions"]

        assert set(m["surfaces"]) <= extension_store._ALLOWED_SURFACES, m["surfaces"]
        assert "runtime_mcp" not in m["surfaces"], m["surfaces"]

        applied = m["entrypoints"]["applied_config"]
        rules = {r["tag"]: r for r in applied["tag_rules"]}
        assert len(rules) == 2, applied["tag_rules"]

        decision = rules["NEEDS_USER_DECISION"]
        assert decision["strip_wrapper"] is True
        assert decision["bold"] is True
        assert decision["font_scale"] == 1.3
        assert decision["highlight"] == {"color": "#d29922", "alpha": 0.18}, decision.get("highlight")
        assert decision["marker"] == {
            "color": "#d29922",
            "tooltip": "Needs your decision",
            "sound": True,
        }, decision.get("marker")
        assert decision["clear_on"] == "view"

        done = rules["ALL_TASKS__DONE"]
        assert done["strip_wrapper"] is True
        assert done["marker"] == {
            "color": "#2563eb",
            "tooltip": "All tasks done",
            "sound": False,
        }, done.get("marker")
        assert done["clear_on"] == "view"
        # Blue dot is a pure signal — no inline text styling.
        assert "bold" not in done and "font_scale" not in done and "highlight" not in done, done

        print("PASS test_user_attention_manifest")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

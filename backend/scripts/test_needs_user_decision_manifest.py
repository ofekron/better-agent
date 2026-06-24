"""Validates the shipped needs-user-decision extension manifest: empty
permissions, no surface outside _ALLOWED_SURFACES, no runtime_mcp, and the
applied_config tag rule parsed correctly.

Run with:
    cd backend && .venv/bin/python scripts/test_needs_user_decision_manifest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-nud-manifest-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402

_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "extensions" / "needs-user-decision" / "better-agent-extension.json"
)


def main() -> int:
    try:
        raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        m = extension_store.validate_manifest(raw)

        assert m["id"] == "ofek-dev.needs-user-decision", m["id"]
        assert m["permissions"] == {}, m["permissions"]

        assert set(m["surfaces"]) <= extension_store._ALLOWED_SURFACES, m["surfaces"]
        assert "runtime_mcp" not in m["surfaces"], m["surfaces"]

        applied = m["entrypoints"]["applied_config"]
        rules = applied["tag_rules"]
        assert len(rules) == 1, rules
        r = rules[0]
        assert r["tag"] == "NEEDS_USER_DECISION", r
        assert r["strip_wrapper"] is True
        assert r["bold"] is True
        assert r["font_scale"] == 1.3
        assert r["highlight"] == {"color": "#ff8c00", "alpha": 0.18}, r.get("highlight")
        assert r["marker"] == {
            "color": "#ff8c00",
            "tooltip": "Needs your decision",
            "sound": True,
        }, r.get("marker")
        assert r["clear_on"] == "view"

        print("PASS test_needs_user_decision_manifest")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

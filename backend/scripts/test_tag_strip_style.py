"""Locks the tag-rule render pass: wrapper stripping, bold, font sentinel,
empty-registry identity, and the no-'<' fast path.

Run with:
    cd backend && .venv/bin/python scripts/test_tag_strip_style.py
"""
from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-tag-style-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_ref_resolver  # noqa: E402


def main() -> int:
    try:
        # Empty registry -> identity.
        file_ref_resolver.set_tag_rules([])
        src = "<NEEDS_USER_DECISION>hi</NEEDS_USER_DECISION>"
        assert file_ref_resolver._apply_tag_rules(src) == src, "empty registry must be identity"

        file_ref_resolver.set_tag_rules([
            {
                "tag": "NEEDS_USER_DECISION",
                "strip_wrapper": True,
                "bold": True,
                "font_scale": 1.3,
                "highlight": {"color": "#ff8c00", "alpha": 0.18},
            }
        ])

        out = file_ref_resolver._apply_tag_rules(src)
        assert "<NEEDS_USER_DECISION>" not in out and "</NEEDS_USER_DECISION>" not in out, \
            f"wrapper not stripped: {out!r}"
        # Bold rides the bcstyle sentinel as a CSS attr (b=1), NOT raw **..**:
        # raw markdown bold would collide with markdown the agent wrote inside
        # the tag and corrupt the emphasis parse.
        assert "**" not in out, f"bold must not be applied as raw markdown: {out!r}"
        assert "[[bcstyle:" in out and "[[/bcstyle]]" in out, \
            f"style sentinel missing: {out!r}"
        assert "b=1" in out, f"bold attr missing from sentinel: {out!r}"
        assert "s=1.3" in out, f"font-scale attr missing: {out!r}"
        assert "bg=#ff8c00" in out and "a=0.18" in out, \
            f"highlight attrs missing: {out!r}"

        # Regression: inner markdown styling must survive untouched. The agent
        # wrote **bold** and `code` inside the tag; the render pass must NOT
        # inject its own ** around them (which produced ****..** and broke the
        # parse — the bug this fix closes).
        rich = (
            "<NEEDS_USER_DECISION>**1. Run** the full `flow` or "
            "**2. Scope it down**</NEEDS_USER_DECISION>"
        )
        rout = file_ref_resolver._apply_tag_rules(rich)
        assert "**1. Run**" in rout and "**2. Scope it down**" in rout, \
            f"inner bold markdown corrupted: {rout!r}"
        assert "`flow`" in rout, f"inner code markdown corrupted: {rout!r}"
        assert "****" not in rout, f"colliding bold markers introduced: {rout!r}"

        # Fast path: no '<' -> identity (no regex work).
        plain = "just plain text no tags"
        assert file_ref_resolver._apply_tag_rules(plain) == plain

        # End-to-end through _rewrite_content_blocks (text blocks only).
        blocks = [{"type": "text", "text": src}]
        file_ref_resolver._rewrite_content_blocks(blocks, cwd=None)
        assert "<NEEDS_USER_DECISION>" not in blocks[0]["text"], blocks[0]["text"]
        assert "[[bcstyle:" in blocks[0]["text"] and "b=1" in blocks[0]["text"], \
            blocks[0]["text"]

        # Thinking blocks are NOT tag-processed.
        think = [{"type": "thinking", "thinking": src}]
        file_ref_resolver._rewrite_content_blocks(think, cwd=None)
        assert think[0]["thinking"] == src, "thinking must not be tag-stripped"

        file_ref_resolver.set_tag_rules([])
        print("PASS test_tag_strip_style")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

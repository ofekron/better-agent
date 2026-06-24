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
            }
        ])

        out = file_ref_resolver._apply_tag_rules(src)
        assert "<NEEDS_USER_DECISION>" not in out and "</NEEDS_USER_DECISION>" not in out, \
            f"wrapper not stripped: {out!r}"
        # Bold wraps the inner text; font sentinel wraps the bolded text so
        # the frontend can split on the sentinel without breaking the **..**.
        assert "**hi**" in out, f"bold not applied: {out!r}"
        assert "[[bcsize:1.3]]" in out and "[[/bcsize]]" in out, \
            f"font sentinel missing: {out!r}"

        # Fast path: no '<' -> identity (no regex work).
        plain = "just plain text no tags"
        assert file_ref_resolver._apply_tag_rules(plain) == plain

        # End-to-end through _rewrite_content_blocks (text blocks only).
        blocks = [{"type": "text", "text": src}]
        file_ref_resolver._rewrite_content_blocks(blocks, cwd=None)
        assert "<NEEDS_USER_DECISION>" not in blocks[0]["text"], blocks[0]["text"]
        assert "**hi**" in blocks[0]["text"], blocks[0]["text"]

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

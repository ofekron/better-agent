"""Regression test: Gemini runner attaches image files via `@path`.

Before the fix, `runner_gemini` ignored the `images` array entirely
(text+images silently dropped the images) and hard-failed image-only
messages. This locks the fix: images are materialized to disk and
referenced with `@path` tokens so the gemini CLI's headless
`handleAtCommand` inlines them as image parts, and an attachment dir is
returned for `--include-directories`.

Quota-independent — does NOT call the gemini API. Run with:
    cd backend && .venv/bin/python scripts/test_gemini_image_attachments.py
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_test_home.isolate("bc_gemini_attach_")

from runner_gemini import _apply_image_attachments  # noqa: E402

# 1x1 red PNG
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4"
    "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _img(media_type: str = "image/png") -> dict:
    return {"media_type": media_type, "data": _PNG_B64}


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if cond:
            _ok(label)
        else:
            print(f"\033[91mFAIL\033[0m  {label}")
            failures.append(label)

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)

        # --- text + image: prompt keeps text AND gains an @ref ---
        prompt, att_dir = _apply_image_attachments(
            run_dir, "what is this?", [_img()]
        )
        att_file = run_dir / "attachments" / "attachment_0.png"
        check("what is this?" in (prompt or ""), "text preserved")
        check(f"@{att_file}" in (prompt or ""), "prompt references @<abs path>")
        check(att_dir == run_dir / "attachments", "returns attachment dir")
        check(att_file.exists(), "attachment file written to disk")
        check(
            att_file.read_bytes() == base64.b64decode(_PNG_B64),
            "attachment bytes match decoded base64",
        )

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        # --- image-only: no longer rejected; prompt is just the @ref ---
        prompt, att_dir = _apply_image_attachments(run_dir, None, [_img()])
        att_file = run_dir / "attachments" / "attachment_0.png"
        check(prompt == f"@{att_file}", "image-only prompt is the @ref")
        check(att_dir == run_dir / "attachments", "image-only returns dir")

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        # --- jpeg extension normalized to .jpg, multiple images ---
        prompt, att_dir = _apply_image_attachments(
            run_dir, "x", [_img("image/jpeg"), _img("image/png")]
        )
        f0 = run_dir / "attachments" / "attachment_0.jpg"
        f1 = run_dir / "attachments" / "attachment_1.png"
        check(f0.exists() and f1.exists(), "multiple images, jpeg->jpg ext")
        check(f"@{f0}" in prompt and f"@{f1}" in prompt, "all images referenced")

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        # --- no images: passthrough, no dir, no attachments folder ---
        prompt, att_dir = _apply_image_attachments(run_dir, "hello", [])
        check(prompt == "hello" and att_dir is None, "no images: passthrough")
        check(
            not (run_dir / "attachments").exists(),
            "no images: no attachments dir created",
        )

    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

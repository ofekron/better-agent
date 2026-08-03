from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_codex_attach_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner_codex import build_codex_steer_input, build_codex_turn_input  # noqa: E402

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4"
    "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _img(media_type: str = "image/png") -> dict:
    return {"media_type": media_type, "data": _PNG_B64}


def _file(name: str = "notes.txt", data: bytes = b"attachment text") -> dict:
    return {
        "name": name,
        "media_type": "text/plain",
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


def test_codex_turn_and_steer_preserve_files(tmp_path):
    file_attachment = _file()
    turn_input = build_codex_turn_input(
        tmp_path, "inspect both", [_img()], [file_attachment],
    )
    assert turn_input[0] == {
        "type": "text",
        "text": '<file name="notes.txt">\nattachment text\n</file>\n\ninspect both',
        "text_elements": [],
    }
    assert turn_input[1]["type"] == "localImage"

    steer_input = build_codex_steer_input(tmp_path, {
        "prompt": "adjust",
        "images": [],
        "files": [file_attachment],
    })
    assert steer_input == [{
        "type": "text",
        "text": '<file name="notes.txt">\nattachment text\n</file>\n\nadjust',
        "text_elements": [],
    }]


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  {'✓' if cond else '✗'} {label}")
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        turn_input = build_codex_turn_input(run_dir, "what is this?", [_img()])
        att_file = run_dir / "attachments" / "attachment_0.png"
        check(
            turn_input == [
                {"type": "text", "text": "what is this?", "text_elements": []},
                {"type": "localImage", "path": str(att_file)},
            ],
            "text plus image becomes text and localImage input parts",
        )
        check(att_file.exists(), "attachment file written")
        check(
            att_file.read_bytes() == base64.b64decode(_PNG_B64),
            "attachment bytes match decoded base64",
        )

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        turn_input = build_codex_turn_input(run_dir, "", [_img("image/jpeg")])
        att_file = run_dir / "attachments" / "attachment_0.jpg"
        check(
            turn_input == [{"type": "localImage", "path": str(att_file)}],
            "image-only turns are valid localImage input",
        )
        check(att_file.exists(), "jpeg attachment written with jpg extension")

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        turn_input = build_codex_turn_input(run_dir, "hello", [])
        check(
            turn_input == [{"type": "text", "text": "hello", "text_elements": []}],
            "no images keeps a text-only turn input",
        )
        check(
            not (run_dir / "attachments").exists(),
            "no images does not create attachments dir",
        )

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        steer_input = build_codex_steer_input(run_dir, {
            "prompt": "look here",
            "images": [_img()],
        })
        att_file = run_dir / "attachments" / "attachment_0.png"
        check(
            steer_input == [
                {"type": "text", "text": "look here", "text_elements": []},
                {"type": "localImage", "path": str(att_file)},
            ],
            "steer payload images become localImage input parts",
        )

    if failures:
        print(f"\nFAILED: {len(failures)} assertion(s)")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

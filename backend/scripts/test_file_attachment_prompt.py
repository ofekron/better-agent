from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from file_attachment_prompt import (  # noqa: E402
    file_attachment_metadata,
    prepend_file_attachments,
    validate_file_attachment,
)


def _file(name: str, raw: bytes, media_type: str = "text/plain") -> dict:
    return {
        "name": name,
        "media_type": media_type,
        "size": len(raw),
        "data": base64.b64encode(raw).decode("ascii"),
    }


def test_text_binary_and_filename_are_encoded_once():
    prompt = prepend_file_attachments("question", [
        _file('a\"<&.txt', b"hello"),
        _file("binary.bin", b"\xff", "application/octet-stream"),
    ])
    assert '<file name="a&quot;&lt;&amp;.txt">\nhello\n</file>' in prompt
    assert '<file name="binary.bin">[binary file, 1 bytes]</file>' in prompt
    assert prompt.endswith("\n\nquestion")


@pytest.mark.parametrize("field,value", [
    ("data", "not-base64"),
    ("size", 999),
])
def test_malformed_attachments_fail_closed(field, value):
    attachment = _file("a.txt", b"hello")
    attachment[field] = value
    with pytest.raises(ValueError):
        prepend_file_attachments("question", [attachment])


def test_prepend_returns_prompt_unchanged_when_no_files():
    assert prepend_file_attachments("question", []) == "question"


def test_prepend_empty_prompt_returns_preamble_only():
    prompt = prepend_file_attachments("", [_file("a.txt", b"hello")])
    assert prompt == '<file name="a.txt">\nhello\n</file>'
    assert not prompt.endswith("\n\n")


def test_validate_returns_name_media_type_size_and_raw():
    name, media_type, size, raw = validate_file_attachment(
        _file("a.txt", b"hello", "image/png"), 0
    )
    assert (name, media_type, size, raw) == ("a.txt", "image/png", 5, b"hello")


@pytest.mark.parametrize("item", [None, 42, "str", ["a"], object()])
def test_validate_rejects_non_dict_item(item):
    with pytest.raises(ValueError, match="Malformed file attachment"):
        validate_file_attachment(item, 0)


@pytest.mark.parametrize("override", [
    {"name": 1}, {"name": ""}, {"name": None},
    {"media_type": 1}, {"media_type": ""}, {"media_type": None},
    {"size": "big"}, {"size": -1}, {"size": None},
    {"data": 1}, {"data": None}, {"data": b"bytes"},
])
def test_validate_rejects_malformed_fields(override):
    attachment = _file("a.txt", b"hello")
    attachment.update(override)
    with pytest.raises(ValueError, match="Malformed file attachment"):
        validate_file_attachment(attachment, 0)


@pytest.mark.parametrize("missing", ["name", "media_type", "size", "data"])
def test_validate_rejects_missing_fields(missing):
    attachment = _file("a.txt", b"hello")
    del attachment[missing]
    with pytest.raises(ValueError, match="Malformed file attachment"):
        validate_file_attachment(attachment, 0)


def test_validate_rejects_size_above_max_size():
    attachment = _file("a.txt", b"hello")
    with pytest.raises(ValueError, match='File "a.txt" exceeds 10 MB limit'):
        validate_file_attachment(attachment, 0, max_size=3)


def test_validate_accepts_size_at_max_size():
    attachment = _file("a.txt", b"hello")
    name, media_type, size, raw = validate_file_attachment(
        attachment, 0, max_size=5
    )
    assert size == 5 and raw == b"hello"


def test_metadata_returns_name_media_type_and_size_without_raw():
    files = [
        _file("a.txt", b"hello", "text/plain"),
        _file("b.png", b"\x89PNG", "image/png"),
    ]
    metadata = file_attachment_metadata(files)
    assert metadata == [
        {"name": "a.txt", "media_type": "text/plain", "size": 5},
        {"name": "b.png", "media_type": "image/png", "size": 4},
    ]


def test_metadata_returns_empty_list_for_no_files():
    assert file_attachment_metadata([]) == []


def test_metadata_delegates_validation_and_raises_on_malformed():
    attachment = _file("a.txt", b"hello")
    attachment["size"] = 999
    with pytest.raises(ValueError, match="size does not match"):
        file_attachment_metadata([attachment])

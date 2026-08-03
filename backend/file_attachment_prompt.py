from __future__ import annotations

import base64
import binascii
from html import escape

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


def prepend_file_attachments(prompt: str, files: list) -> str:
    if not files:
        return prompt
    sections = [_file_section(item, index) for index, item in enumerate(files)]
    preamble = "\n\n".join(sections)
    return f"{preamble}\n\n{prompt}" if prompt else preamble


def _file_section(item: object, index: int) -> str:
    name, _media_type, size, raw = validate_file_attachment(item, index)
    safe_name = escape(name, quote=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f'<file name="{safe_name}">[binary file, {size} bytes]</file>'
    return f'<file name="{safe_name}">\n{text}\n</file>'


def validate_file_attachment(
    item: object,
    index: int,
    *,
    max_size: int | None = None,
) -> tuple[str, str, int, bytes]:
    if not isinstance(item, dict):
        raise ValueError("Malformed file attachment")
    name = item.get("name")
    media_type = item.get("media_type")
    size = item.get("size")
    data = item.get("data")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(media_type, str)
        or not media_type
        or not isinstance(size, int)
        or size < 0
        or not isinstance(data, str)
    ):
        raise ValueError("Malformed file attachment")
    if max_size is not None and size > max_size:
        raise ValueError(f'File "{name}" exceeds 10 MB limit')
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ValueError(f'File "{name}" has invalid attachment data') from exc
    if len(raw) != size:
        raise ValueError(f'File "{name}" size does not match its data')
    return name, media_type, size, raw


def file_attachment_metadata(files: list) -> list[dict]:
    metadata = []
    for index, item in enumerate(files):
        name, media_type, size, _raw = validate_file_attachment(item, index)
        metadata.append({
            "name": name,
            "media_type": media_type,
            "size": size,
        })
    return metadata

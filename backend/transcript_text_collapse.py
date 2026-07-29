from __future__ import annotations

import hashlib
import re


def normalize_repeated_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    return limit


def raw_index_after_normalized_prefix_checked(text: str, prefix_len: int) -> tuple[int, bool]:
    normalized_len = 0
    emitted_any = False
    in_whitespace = False
    for index, char in enumerate(text):
        if char.isspace():
            if emitted_any and not in_whitespace:
                if normalized_len >= prefix_len:
                    return index, True
                normalized_len += 1
                in_whitespace = True
            continue
        emitted_any = True
        in_whitespace = False
        if normalized_len >= prefix_len:
            return index, True
        normalized_len += 1
        if normalized_len >= prefix_len:
            return index + 1, True
    return len(text), normalized_len >= prefix_len


def raw_index_after_normalized_prefix(text: str, prefix_len: int) -> int:
    raw_index, _reached = raw_index_after_normalized_prefix_checked(text, prefix_len)
    return raw_index

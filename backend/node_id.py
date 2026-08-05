from __future__ import annotations

import re


NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_node_id(value: object) -> str:
    if type(value) is not str or not NODE_ID_RE.fullmatch(value):
        raise ValueError("machine node id is invalid")
    return value

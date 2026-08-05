"""Wire serialization for surface_contract dataclasses (ADR 0006 §2/§4).

Pure and stdlib-only: recursively turns dataclasses/enums/tuples into
JSON-safe dicts/lists/scalars. Nulls are kept (lossless) rather than
omitted — the transport layer decides presentation, this module only
removes non-JSON types."""

from __future__ import annotations

import dataclasses
from enum import Enum


def to_wire(obj: object) -> object:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_wire(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (tuple, list)):
        return [to_wire(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_wire(v) for k, v in obj.items()}
    return obj

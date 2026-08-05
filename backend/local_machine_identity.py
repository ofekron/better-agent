from __future__ import annotations

import threading

from env_compat import get_env
from node_id import validate_node_id


_LOCK = threading.Lock()
_local_machine_id: str | None = None


def initialize_local_machine_id(node_id: object) -> str:
    value = validate_node_id(node_id)
    global _local_machine_id
    with _LOCK:
        if _local_machine_id is not None and _local_machine_id != value:
            raise RuntimeError("local machine identity is already initialized")
        _local_machine_id = value
    return value


def initialize_primary_machine_id() -> str:
    if get_env("BETTER_CLAUDE_TOPOLOGY_PATH"):
        from topology import local_node_id

        return initialize_local_machine_id(local_node_id())
    return initialize_local_machine_id("primary")


def require_local_machine_id() -> str:
    with _LOCK:
        if _local_machine_id is None:
            raise RuntimeError("local machine identity is not initialized")
        return _local_machine_id


def require_matching_local_machine_id(node_id: object) -> str:
    admitted = validate_node_id(node_id)
    if admitted != require_local_machine_id():
        raise ValueError("local machine identity does not match admitted routing")
    return admitted

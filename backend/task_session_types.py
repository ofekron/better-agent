NORMAL = "normal"
PROVISIONED_DIRECT = "provisioned_direct"
PROVISIONED_FORK = "provisioned_fork"
PROVISIONED_FORK_WITH_MEMORY = "provisioned_fork_with_memory"

VALID = (
    NORMAL,
    PROVISIONED_DIRECT,
    PROVISIONED_FORK,
    PROVISIONED_FORK_WITH_MEMORY,
)
PROVISIONED = frozenset(VALID[1:])
FORKED = frozenset((PROVISIONED_FORK, PROVISIONED_FORK_WITH_MEMORY))


def has_memory(session_type: str) -> bool:
    return session_type == PROVISIONED_FORK_WITH_MEMORY

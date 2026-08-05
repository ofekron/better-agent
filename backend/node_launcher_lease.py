from __future__ import annotations

from primary_launcher_lease import (
    PrimaryLauncherBusyError,
    PrimaryLauncherLease,
    PrimaryLauncherLeaseError,
)


class NodeLauncherLease(PrimaryLauncherLease):
    role = "node"
    lock_name = "node-launcher.lock"
    handoff_env_keys = (
        "BETTER_AGENT_NODE_LAUNCHER_HANDOFF_FD",
        "BETTER_AGENT_NODE_LAUNCHER_HANDOFF_ID",
        "BETTER_AGENT_NODE_LAUNCHER_HANDOFF_TOKEN",
    )


class NodeServiceManagementLease(PrimaryLauncherLease):
    role = "node-service-management"
    lock_name = "node-service-management.lock"
    handoff_env_keys = (
        "BETTER_AGENT_NODE_SERVICE_HANDOFF_FD",
        "BETTER_AGENT_NODE_SERVICE_HANDOFF_ID",
        "BETTER_AGENT_NODE_SERVICE_HANDOFF_TOKEN",
    )


class NodeUpdateLease(PrimaryLauncherLease):
    role = "node-update"
    lock_name = "node-update.lock"
    handoff_env_keys = (
        "BETTER_AGENT_NODE_UPDATE_HANDOFF_FD",
        "BETTER_AGENT_NODE_UPDATE_HANDOFF_ID",
        "BETTER_AGENT_NODE_UPDATE_HANDOFF_TOKEN",
    )


NodeLauncherBusyError = PrimaryLauncherBusyError
NodeLauncherLeaseError = PrimaryLauncherLeaseError

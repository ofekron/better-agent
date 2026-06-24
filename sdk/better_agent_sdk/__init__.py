"""Better Agent Integration SDK.

The supported way for an integration subprocess (extension MCP server,
backend route handler, or first-party orchestrator) to reach core state. The
runtime puts this package on each extension subprocess's PYTHONPATH and
injects auth env vars:

  BETTER_AGENT_BACKEND_URL, BETTER_AGENT_INTERNAL_TOKEN,
  BETTER_AGENT_APP_SESSION_ID, BETTER_AGENT_CWD, BETTER_AGENT_EXTENSION_ID,
  BETTER_AGENT_MODEL, BETTER_AGENT_PROVIDER_ID

Legacy BETTER_CLAUDE_* names are still accepted.

Integrations stay sandboxed: they can ``import better_agent_sdk`` but cannot
import core modules directly. This package exposes ONLY generic core substrate
— sessions, team activation, provisioned runs, per-session events,
ownership-gated message mutation, settings/config — over core's loopback
``/api/internal/*`` API.

Feature-specific capabilities (requirements, scheduler, credentials,
browser-harness, project-structure, etc.) are owned by their extensions, which
ship their own SDKs. One extension reaches another through
``Client.call_extension``; core routes the call without baking in feature logic.
"""
from better_agent_sdk.client import BetterAgentError, Client
from better_agent_sdk.manifest import (
    Badge,
    ExtensionProtocol,
    FrontendModule,
    HookAction,
    Instruction,
    McpPredicate,
    McpServer,
    Page,
    PermissionSet,
    QuickButton,
    Setting,
    SmokeTest,
    TeamDefinition,
)

IntegrationClient = Client

__all__ = [
    "Client",
    "IntegrationClient",
    "BetterAgentError",
    "Badge",
    "ExtensionProtocol",
    "FrontendModule",
    "HookAction",
    "Instruction",
    "McpPredicate",
    "McpServer",
    "Page",
    "PermissionSet",
    "QuickButton",
    "Setting",
    "SmokeTest",
    "TeamDefinition",
]

"""Chat Surface Contract family (ADRs 0006-0009): the provider/execution-
agnostic boundary the UI consumes. See better-agent-private/documents/adr/."""

from backend.surface_contract.adapter import BetterAgentAdapter
from backend.surface_contract.chat_surface import ChatSurface
from backend.surface_contract.identity import CONTRACT_VERSION
from backend.surface_contract.provider_config_surface import ProviderConfigSurface
from backend.surface_contract.runs_surface import RunsSurface
from backend.surface_contract.session_surface import SessionSurface

__all__ = [
    "CONTRACT_VERSION",
    "BetterAgentAdapter",
    "ChatSurface",
    "ProviderConfigSurface",
    "RunsSurface",
    "SessionSurface",
]

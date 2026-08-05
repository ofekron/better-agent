"""The composed adapter: the single provider/execution-agnostic runtime
object the UI-facing transport (REST/WS handlers) delegates to. Concrete by
design — composition only; the four surfaces are the abstract seams."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.surface_contract.chat_surface import ChatSurface
from backend.surface_contract.identity import CONTRACT_VERSION
from backend.surface_contract.provider_config_surface import ProviderConfigSurface
from backend.surface_contract.runs_surface import RunsSurface
from backend.surface_contract.session_surface import SessionSurface


@dataclass(frozen=True, slots=True)
class BetterAgentAdapter:
    chat: ChatSurface
    providers: ProviderConfigSurface
    sessions: SessionSurface
    runs: RunsSurface
    contract_version: int = field(default=CONTRACT_VERSION, init=False)

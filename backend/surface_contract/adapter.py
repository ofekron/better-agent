"""The composed adapter ABC: the single provider/execution-agnostic seam
the UI-facing transport (REST/WS handlers) delegates to. One surface per
contract ADR; no chat state may travel outside these surfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.surface_contract.chat_surface import ChatSurface
from backend.surface_contract.identity import CONTRACT_VERSION
from backend.surface_contract.provider_config_surface import ProviderConfigSurface
from backend.surface_contract.runs_surface import RunsSurface
from backend.surface_contract.session_surface import SessionSurface


class BetterAgentAdapter(ABC):
    contract_version: int = CONTRACT_VERSION

    @property
    @abstractmethod
    def chat(self) -> ChatSurface: ...

    @property
    @abstractmethod
    def providers(self) -> ProviderConfigSurface: ...

    @property
    @abstractmethod
    def sessions(self) -> SessionSurface: ...

    @property
    @abstractmethod
    def runs(self) -> RunsSurface: ...

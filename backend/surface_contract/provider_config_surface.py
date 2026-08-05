"""Provider config & auth surface ABC (ADR 0007). Settings render entirely
from descriptors; secrets are write-only and never appear in read models."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.surface_contract.descriptors import (
    InstallableDescriptor,
    ModelCatalog,
    ProviderDescriptor,
    RuntimeProfile,
)
from backend.surface_contract.identity import Emit, ProviderId, Subscription
from backend.surface_contract.intents import ProviderIntent, TransportAck


class ProviderConfigSurface(ABC):
    @abstractmethod
    def list_providers(self) -> tuple[ProviderDescriptor, ...]: ...

    @abstractmethod
    def installable_catalog(self) -> tuple[InstallableDescriptor, ...]: ...

    @abstractmethod
    def model_catalog(self, provider_id: ProviderId) -> ModelCatalog: ...

    @abstractmethod
    def runtime_profiles(self) -> tuple[RuntimeProfile, ...]: ...

    @abstractmethod
    def subscribe(self, emit: Emit) -> Subscription:
        """emit receives ProviderFrame values, change-only."""

    @abstractmethod
    def submit(self, intent: ProviderIntent) -> TransportAck: ...

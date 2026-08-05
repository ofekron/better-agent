"""The only module implementing backend/surface_contract/ surfaces.
See backend/scripts/test_adapter_boundaries.py for the enforced import
boundary."""

from backend.adapters.chat_adapter import ChatSurfaceAdapter
from backend.adapters.projection import BusBoundProjection, SurfaceProjection
from backend.adapters.provider_adapter import ProviderConfigSurfaceAdapter
from backend.adapters.runs_adapter import RunsSurfaceAdapter
from backend.adapters.session_adapter import SessionSurfaceAdapter
from backend.surface_contract.adapter import BetterAgentAdapter

__all__ = [
    "BusBoundProjection",
    "SurfaceProjection",
    "ChatSurfaceAdapter",
    "ProviderConfigSurfaceAdapter",
    "RunsSurfaceAdapter",
    "SessionSurfaceAdapter",
    "build_adapter",
]


def build_adapter() -> BetterAgentAdapter:
    """Compose the four concrete surfaces into one `BetterAgentAdapter`
    and bind each one's live-plane bus subscriptions. Each concrete
    `bind()` is independently idempotent (re-subscribes under
    deterministic per-pattern names), but this factory itself mints a
    FRESH set of adapter instances every call — the composition root
    (backend/main.py) calls it exactly once at startup."""
    chat = ChatSurfaceAdapter()
    providers = ProviderConfigSurfaceAdapter()
    sessions = SessionSurfaceAdapter()
    runs = RunsSurfaceAdapter()
    chat.bind()
    providers.bind()
    sessions.bind()
    runs.bind()
    return BetterAgentAdapter(chat=chat, providers=providers, sessions=sessions, runs=runs)

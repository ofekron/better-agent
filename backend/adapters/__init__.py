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


def build_adapter(command_port=None) -> BetterAgentAdapter:
    """Compose the four concrete surfaces into one `BetterAgentAdapter`
    and bind each one's live-plane bus subscriptions. Each concrete
    `bind()` is independently idempotent (re-subscribes under
    deterministic per-pattern names), but this factory itself mints a
    FRESH set of adapter instances every call — the composition root
    (backend/main.py) calls it exactly once at startup.

    `command_port` (a `backend.adapters.command_port.ChatCommandPort`,
    typically built by `surface_commands.build_chat_command_port`) is
    wired onto the chat surface's `_command_port` attribute rather than
    through a constructor/setter — `ChatSurfaceAdapter`'s own edit
    surface for this migration is `submit()` only, so injection happens
    here, at the one call site that already owns both the fresh instance
    and the port to give it."""
    chat = ChatSurfaceAdapter()
    if command_port is not None:
        chat._command_port = command_port
    providers = ProviderConfigSurfaceAdapter()
    sessions = SessionSurfaceAdapter()
    runs = RunsSurfaceAdapter()
    chat.bind()
    providers.bind()
    sessions.bind()
    runs.bind()
    return BetterAgentAdapter(chat=chat, providers=providers, sessions=sessions, runs=runs)

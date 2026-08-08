"""The only module implementing backend/surface_contract/ surfaces.
See backend/scripts/test_adapter_boundaries.py for the enforced import
boundary.

CANONICALIZATION: this backend imports its infra modules bare (`import
event_bus`), while backend/adapters/* and backend/adapter_api.py import
dotted (`backend.event_bus`) per the boundary that test file enforces.
Left alone, that mismatch mints a second EventBus (etc.) singleton under
the "backend.*" module keys the first time anything does `from backend
import event_bus` — so e.g. ChatSurfaceAdapter.bind() would subscribe on
a bus nothing else ever publishes to.

Alias the bare-imported infra singletons this package's adapters actually
use onto their "backend.*" sys.modules keys HERE, at package-import time,
before any submodule below performs its own dotted `from backend.xxx
import ...` — so every dotted import resolves to the SAME
already-bare-imported instance instead of loading a fresh module. This is
the ONE sanctioned sys.modules-mutation site for backend/adapters/* (see
backend/scripts/test_adapter_boundaries.py's sys.modules-mutation check),
alongside store_access.py's `_resolve` (a narrower, store-specific version
of the same problem — see that module's docstring for why it stays
separate and dynamic instead of joining this list).

`event_ingester` is canonicalized here even though no backend/adapters/*.py
file itself imports it dotted — the coverage this list provides isn't
limited to this package's own internal imports. backend/scripts/
test_chat_adapter.py and test_owner_facts.py dotted-import
`backend.event_ingester` directly to prove a real single-writer invariant
(the ingester and the ChatSurfaceAdapter they exercise must observe the
same in-process EventIngester), and that only holds if this canonicalizes
it too — see backend/scripts/test_adapter_boundaries.py's
`test_outward_dotted_imports_have_alias_coverage`, which fails on any
future dotted `backend.<stateful module>` import anywhere in the repo that
this list (or store_access.py's dynamic set) doesn't cover.

Self-canonicalizing on purpose: Python guarantees a package's __init__.py
runs before any of its submodules, and runs at most once per process — so
this fires no matter which permitted importer (backend/main.py,
backend/adapter_api.py, backend/surface_commands.py, or a
backend/scripts/test_*.py) imports backend.adapters first. Previously this
lived inline in backend/main.py's `_wire_surface_adapter`, which only
protected callers that ran AFTER that function — any earlier import of
backend.adapters (e.g. a test importing it directly, as several
backend/scripts/test_*.py already do) got unaliased, disconnected
singletons. Moving it here removes that import-order hazard entirely.

Both `sys.modules[...]` AND the `backend` package's own attribute must be
repointed: `import backend.x as y` compiles to an IMPORT_FROM opcode,
which resolves via `getattr(sys.modules["backend"], "x")` FIRST and only
falls back to a `sys.modules["backend.x"]` lookup if that attribute is
absent (see CPython's `import_from` in ceval.c). `from backend.x import y`
is immune (it always re-checks `sys.modules` by fully-qualified name), but
if anything anywhere does a real dotted-attribute-style `import backend.x`
BEFORE this package is first imported, `sys.modules["backend"].x` gets set
to that real module and a `sys.modules` dict-only reassignment can never
override it again — the getattr fast path keeps winning. Setting the
attribute here too closes that gap for every `import backend.x as y` site,
not only the `sys.modules` dict lookups.
"""

import sys as _sys

import event_bus as _event_bus
import event_ingester as _event_ingester
import event_journal as _event_journal
import paths as _paths
import perf as _perf
import scheme_migrations as _scheme_migrations

_backend_pkg = _sys.modules["backend"]
for _bare_name, _module in (
    ("event_bus", _event_bus),
    ("event_ingester", _event_ingester),
    ("event_journal", _event_journal),
    ("paths", _paths),
    ("perf", _perf),
    ("scheme_migrations", _scheme_migrations),
):
    _sys.modules[f"backend.{_bare_name}"] = _module
    setattr(_backend_pkg, _bare_name, _module)
del _bare_name, _module, _sys, _backend_pkg, _event_bus, _event_ingester, _event_journal, _paths, _perf, _scheme_migrations

from backend.adapters.chat_adapter import ChatSurfaceAdapter
from backend.adapters.projection import BusBoundProjection, SurfaceProjection
from backend.adapters.provider_adapter import ProviderConfigSurfaceAdapter
from backend.adapters.runs_adapter import RunsSurfaceAdapter
from backend.adapters.session_adapter import SessionSurfaceAdapter
from backend.adapters.system_adapter import SystemSurfaceAdapter
from backend.surface_contract.adapter import BetterAgentAdapter

__all__ = [
    "BusBoundProjection",
    "SurfaceProjection",
    "ChatSurfaceAdapter",
    "ProviderConfigSurfaceAdapter",
    "RunsSurfaceAdapter",
    "SessionSurfaceAdapter",
    "SystemSurfaceAdapter",
    "build_adapter",
]


def build_adapter(
    command_port=None, session_command_port=None, system_command_port=None,
) -> BetterAgentAdapter:
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
    and the port to give it. `session_command_port` (a
    `backend.adapters.command_port.SessionCommandPort`, typically built by
    `session_commands.build_session_command_port`) is wired onto the
    session surface's `_command_port` attribute the same way.
    `system_command_port` (a `backend.adapters.command_port.SystemCommandPort`,
    typically built by `system_commands.build_system_command_port`) is
    wired onto the system surface's `_command_port` attribute the same
    way (ADR 0011)."""
    chat = ChatSurfaceAdapter()
    if command_port is not None:
        chat._command_port = command_port
    providers = ProviderConfigSurfaceAdapter()
    sessions = SessionSurfaceAdapter()
    if session_command_port is not None:
        sessions._command_port = session_command_port
    runs = RunsSurfaceAdapter()
    system = SystemSurfaceAdapter()
    if system_command_port is not None:
        system._command_port = system_command_port
    chat.bind()
    providers.bind()
    sessions.bind()
    runs.bind()
    system.bind()
    return BetterAgentAdapter(
        chat=chat, providers=providers, sessions=sessions, runs=runs, system=system,
    )

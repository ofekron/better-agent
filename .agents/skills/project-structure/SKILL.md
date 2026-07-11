---
name: project-structure
description: Use for Better Agent repo orientation before looking for backend, frontend, provider, state, logs, tests, or instruction-file locations in this Python/FastAPI, React, Electron, and provider-integration codebase.
---

# Better Agent Project Structure

Better Agent is a multi-provider desktop/web agent runtime. The backend owns durable state, subprocess orchestration, provider adapters, recovery, event ingestion, extensions, and configuration sync. The frontend reflects backend state through REST snapshots and WebSocket events.

Runtime profile is the formal name for a provider/model/reasoning-effort selection resolved through that provider's runner.

## Routing

- `backend/`: FastAPI backend, provider runners/adapters, provider runtime policy, stores, orchestration, recovery, event ingestion, permissions, extensions, and test scripts.
- `backend/bff_server.py` + `backend/bff_app_routes.py`: browser-facing Better Agent application boundary. App-only drafts, UI-selection state, and presentation preferences are owned here; execution preferences use the typed authenticated `bff_runtime_service.py` contract. Unmatched runtime operations are forwarded to the internal runtime endpoint.
- `backend/capability_api.py`: capability/action registry for extension-to-core calls; extensions use the SDK's pathless `invoke_capability` substrate and manifest grants rather than raw internal routes.
- `backend/extension_jobs.py`: core-owned durable async workflow registry for extension jobs; owns persisted job lifecycle, restart resume, completion polling, and delegation-result recovery while extensions own domain parsing/policy.
- `backend/todo_projection.py`: provider-neutral event-to-todo/task projection owned by core session replay and reused by the Todos extension.
- `backend/assistant_ui.py`: Assistant extension substrate; provisions the visible `Assistant` session and hidden `Assistant Monitor` session that sync through the extension board/store.
- `backend/extension_context_audit.py`: non-blocking, cache-backed provisioned-session audit of installed extension harness contributions; injected as dynamic runtime context when a fresh cached audit exists.
- `backend/tailscale_https.py`: Tailscale status/health helper for preferring verified `https://*.ts.net` external URLs with local fallback.
- `frontend/`: React UI, session/workspace views, settings, i18n, hooks, and UI tests.
- `extensions/`: bundled Better Agent extensions and their backend/MCP surfaces.
- `daemonhost/switch_control.py`: core-owned serialized line-switch requests, journal, crash recovery, and pointer rollback; the Switch Control extension is UI/capability-only.
- `provider-config-sync/`: source checkout for provider capability/config synchronization across Codex, Claude, and Gemini. Better Agent runtime/build consumers use pinned artifacts under `vendor/provider-config-sync/`, never source-path injection.
- Private extensions are installed packages discovered through persisted manifests. Public core must not import or probe the nested `better-agent-private` source tree.
- Root instruction files: `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` hold provider-facing repo instructions.
- Tests: backend integration scripts live under `backend/scripts/`; frontend tests live under `frontend/tests/`.
- Persistent Better Agent state must route through `backend/paths.py::bc_home()` and honor `BETTER_AGENT_HOME`.

## Keeping This Skill Current

Agents must update this skill when material project facts change: major directories, ownership boundaries, persistent state locations, provider surfaces, run/test strategy, or project-specific invariants. Keep it compact and current-state only.

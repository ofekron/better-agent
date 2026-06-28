---
name: project-structure
description: Use for Better Agent repo orientation before looking for backend, frontend, provider, state, logs, tests, or instruction-file locations in this Python/FastAPI, React, Electron, and provider-integration codebase.
---

# Better Agent Project Structure

Better Agent is a multi-provider desktop/web agent runtime. The backend owns durable state, subprocess orchestration, provider adapters, recovery, event ingestion, extensions, and configuration sync. The frontend reflects backend state through REST snapshots and WebSocket events.

## Routing

- `backend/`: FastAPI backend, provider runners/adapters, stores, orchestration, recovery, event ingestion, permissions, extensions, and test scripts.
- `frontend/`: React UI, session/workspace views, settings, i18n, hooks, and UI tests.
- `extensions/`: bundled Better Agent extensions and their backend/MCP surfaces.
- `provider-config-sync/`: separate checkout for provider capability/config synchronization across Codex, Claude, and Gemini.
- Root instruction files: `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` hold provider-facing repo instructions.
- Tests: backend integration scripts live under `backend/scripts/`; frontend tests live under `frontend/tests/`.
- Persistent Better Agent state must route through `backend/paths.py::bc_home()` and honor `BETTER_AGENT_HOME`.

## Keeping This Skill Current

Agents must update this skill when material project facts change: major directories, ownership boundaries, persistent state locations, provider surfaces, run/test strategy, or project-specific invariants. Keep it compact and current-state only.

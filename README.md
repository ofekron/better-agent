# Better Agent

**A local desktop workspace for coding agents.**

Better Agent brings Claude, Codex, Gemini, and compatible local or API-backed
providers into one interface built for real coding work: local sessions, visible
tool calls, file context, traces, session history, folder trees, tags, search,
and browser access from the machines you choose on your local network.

Stop juggling terminal tabs, provider UIs, hidden logs, and one-off sessions.
Keep the work local, inspectable, and shaped around how you actually build.

---

## Why Better Agent

| You're stuck with… | Better Agent gives you… |
| --- | --- |
| Scattered terminals, tabs, and provider UIs | One **hosted local workspace** for projects, sessions, providers, file context, and live output |
| A coding agent trapped on one screen | **Local-network access** from other trusted devices that can reach your Better Agent backend |
| Hidden or scattered agent work | **Traceable sessions** with live output, tool calls, and persistent history |
| One model workflow for every task | **Bring your providers**: Claude, Codex, Gemini, and compatible provider setups |
| Workspace state spread across tools | **All provider sessions** organized together with projects, folders, tags, and search |
| A browser-only workflow | **Browser, native desktop, and mobile app** surfaces backed by the same local server |

---

## Core Repo Powers

These are the core Better Agent capabilities in this repository. along with providing
powerfull extensibility.

- **Local ownership** — backend, frontend, sessions, settings, and app packages
  run from your machine; Better Agent does not host your workspace data.
- **LAN-ready hosting** — choose local-only access or bind the backend for
  trusted local-network devices during setup. all backend endpoints gated by authentication of credentials owned by you, working with your os keychain.
- **Browser access** — run the backend once and use Better Agent from the
  browser on your main machine, another computer, or a mobile device on the
  same trusted network.
- **Native app options** — use the browser-first app, the packaged macOS/Windows
  desktop app, or Capacitor mobile apps that point at your Better Agent server.
- **Provider choice** — Quickly configure the AI accounts and CLIs you want to use, having Better Agent accelrate the process, then pick the provider and model per session while keeping every provider's
  sessions in one workspace.
- **Session navigation** — keep last-opened sessions close, switch between
  active session tabs, and reopen recent work without digging through terminal
  scrollback.
- **Folder trees and tags** — arrange sessions into project-specific folder
  trees, assign tags, and filter by the way the work is actually structured.
- **Search and advanced search** — filter sessions quickly, then use richer
  search when names, tags, folders, providers, or remembered context are not
  enough.
- **Live agent view** — keep provider output, tool calls, file context, and
  session history visible in one place. grouped by turns viewing your prompts alongside final agent responses.
- **Project memory** — sessions are organized by working directory and remain
  easy to reopen, search, inspect, fork, and continue.
- **Headless control** — use Better Agent programmatically through the Integration SDK and CLI commands, driving the orchestration layer from scripts and external tools.
- **Extension surface** — private or marketplace extensions can add specialized
  workflows on top of the core app without defining the core experience.

## Integration SDK

Better Agent ships a public Integration SDK in `sdk/better_agent_sdk/`.
Extension backend routes, MCP servers, and first-party orchestrators such as
TestApe run out of process, get only the SDK on `PYTHONPATH`, and call
authenticated core loopback endpoints instead of importing backend modules
directly.

The public `extensions/` directory includes two powerfull guided examples:

- `extensions/ask` — UI to run an agent to search through your sessions to find a best match for a task, moving the task with ease to it to work on.
- `extensions/session-bridge` — adds an MCP surface for cross-session search,
  recall, session proposals, and delegated session work through the SDK.

Contributors can use the same SDK and manifest system to add backend routes,
frontend modules, runtime MCP tools, instruction blocks, settings, storage, and
permission-scoped access to core session state.

## Access And Auth

Better Agent asks for a username and password when the server is first opened.
Those credentials are stored through the platform credential store, and normal
API access is gated after setup:

- Browser sessions use the signed `bc_session` cookie.
- Capacitor mobile clients receive a signed bearer token after login;
  REST calls send it as `Authorization: Bearer ...`, and WebSocket connections
  pass it as a token query parameter because browser WebSocket APIs cannot set
  custom auth headers.
- `/api/internal/*` loopback routes are not public user routes; extension and
  runner subprocesses must send `X-Internal-Token`.
- Public unauthenticated routes are limited to setup/login/logout and served
  desktop/update artifacts.

---

## Architecture at a glance

```
        Browser / desktop shell / mobile app
        local machine or trusted LAN device
                 │  WebSocket + REST
                 ▼
   FastAPI backend  ──►  per-session Claude Code / Gemini runners (detached)
        │                         │
        │  sessions, projects,    ▼
        │  provider settings, tool output

   Backend is the single source of truth — disk-persisted, crash-recoverable.
   Frontend reflects it via pull (REST snapshots) + push (WS deltas).
```

- **Backend:** Python + FastAPI (`backend/`) — event bus, orchestration strategies, persistence stores, provider/runner layer, distributed node protocol.
- **Frontend:** React + TypeScript + Vite (`frontend/`) — three-panel layout (sessions · chat · files), single auto-reconnecting WebSocket, tree-aware session state.

Want the deep dive? See the `project-structure` skill at `.claude/skills/project-structure/`.

---

## Getting started

**Prerequisites:** macOS (auth uses the login keychain), Python 3 with a `backend/.venv`, Node.js, and a provider CLI such as Claude, Codex, or Gemini. See `INSTALL.md` for the full clean-clone, LAN, desktop, mobile, and reset-auth flow.

```bash
# One command — builds the frontend, starts the backend on :8000, serves the UI.
./run.sh
```

First run prompts once for a username + password (stored only in your macOS keychain) and asks whether Better Agent should listen only on this computer or on your local network. Open the printed URL from any trusted device that can reach the backend.

Better Agent can execute commands, read and write files, run provider CLIs, and
persist session data. Run it only in trusted environments, keep backups, review
tool calls, do not expose the backend to untrusted networks, and read
`DISCLAIMER.md`, `SECURITY.md`, and `PUBLICATION_CHECKLIST.md` before public or
shared use.

Need to run the pieces yourself or use the headless CLI? It's all in `.claude/skills/project-structure/sections/running.md`:

```bash
# Backend (dev, hot-reload)
cd backend && source .venv/bin/activate && uvicorn main:app --reload --port 8000

# Frontend (Vite dev server)
cd frontend && npm run dev

# Headless CLI driver
cd backend && source .venv/bin/activate && python cli.py -p "do the thing"

# Reset stored credentials
./run.sh --reset-auth
```

---

## Tests

Integration tests spin up **real** Claude CLI subprocesses — slow, but they catch the wiring bugs mocks never will.

```bash
cd backend && source .venv/bin/activate
python scripts/integration_test_<thing>.py
```

## License

Better Agent is source-available for non-commercial use. Commercial use,
commercial distribution, hosted offerings, and Better Agent marketplaces require
prior written permission from Ofek Ron. It is not OSI-approved open-source
software because commercial rights are reserved. See `LICENSE`.

See `DISCLAIMER.md` for risk and liability notices, `SECURITY.md` for private
vulnerability reporting, `TRADEMARKS` for Better Agent branding and marketplace
naming rules, `NOTICE` for copyright and marketplace trust-root notes, and
`CONTRIBUTING.md` before submitting changes. See `RELEASE.md` for release
integrity requirements and `ROADMAP.md` for the public non-commercial roadmap.

---

**Better Agent — because one agent in a terminal was never going to be enough.**

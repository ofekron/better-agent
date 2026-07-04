# Better Agent

**One cockpit for all your coding agents.**

Claude, Codex, and Gemini — same workspace, same sessions, same tools. Pick the
right agent per task instead of the one your terminal happens to have open. Run
it on your machine, reach it from any device you trust, and never lose a
session again.

Stop juggling terminal tabs, provider UIs, hidden logs, and one-off sessions.
Your agents, your machine, your rules.

---

## Why Better Agent

**Switch providers, keep everything.** A session started with Claude sits next
to one running on Codex and another on Gemini — one workspace, one history, one
set of projects, folders, tags, and search. Pick provider and model per session;
nothing about your workflow changes.

**Close the laptop — the work continues.** Agents run as detached processes
that outlive your browser tab, your frontend, even a backend restart. Kick off
a long task, walk away, come back to the finished result with the full trace of
what happened while you were gone.

**Work from anywhere in the house.** The backend runs once on your machine;
the UI reaches it from your browser, the packaged macOS/Windows desktop app, or
the mobile app on any trusted device on your network. Start a task at your desk,
check on it from the couch.

**See everything your agents do.** Live output, every tool call, file context,
and full session history in one place — grouped by turns, your prompt alongside
the agent's answer. No hidden logs, no scrollback archaeology.

**A team, not a chatbot.** Fork sessions, delegate work, run agents in
parallel, and drive the whole thing headlessly from scripts through the
Integration SDK and CLI.

**Yours, actually.** Everything — sessions, settings, credentials, data — lives
on your machine. Access is gated by credentials in your OS keychain. Better
Agent hosts nothing.

---

## What's in the box

- **Provider choice** — quick guided setup for the AI accounts and CLIs you
  already have; provider and model picked per session, every provider's
  sessions in one workspace.
- **Sessions that survive** — detached runners keep working through frontend
  disconnects and backend restarts; on startup, in-flight work is recovered and
  reconciled, not lost.
- **Offline-first capture** — type prompts and create sessions even while the
  backend is unreachable; they queue locally and sync when it's back.
- **Organization that scales** — project folder trees, tags, session tabs,
  quick filter, and advanced search across names, tags, folders, providers,
  and remembered context.
- **Live agent view** — provider output, tool calls, and file context streamed
  into a persistent, inspectable history.
- **Project memory** — sessions organized by working directory, easy to reopen,
  fork, and continue.
- **Every surface** — browser, packaged macOS/Windows desktop app, and
  Capacitor mobile apps, all backed by the same local server.
- **Headless control** — drive the orchestration layer from scripts and
  external tools via the Integration SDK and CLI.
- **Extension surface** — private or marketplace extensions add specialized
  workflows on top of the core app. See [EXTENSIONS.md](EXTENSIONS.md) for
  authoring your own and installing marketplace ones.

## Integration SDK

Better Agent ships a public Integration SDK in `sdk/better_agent_sdk/`.
Extension backend routes, MCP servers, and first-party orchestrators such as
TestApe run out of process, get only the SDK on `PYTHONPATH`, and call
authenticated core loopback endpoints instead of importing backend modules
directly.

The public `extensions/` directory includes two guided examples:

- `extensions/ask` — UI that runs an agent to search your sessions for the best
  match for a task, then moves the task there to continue the work.
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

**Better Agent — one agent in a terminal was never going to be enough.**

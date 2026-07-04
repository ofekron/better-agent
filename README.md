# Better Agent

**Run every coding agent. Keep every session.**

Claude, Codex, and Gemini in one local workspace — and stop losing sessions to
closed tabs and dead terminals.

Stop juggling terminal tabs, provider UIs, hidden logs, and throwaway sessions.

*Source-available for non-commercial use — see [License](#license).*

## Quickstart

```bash
git clone https://github.com/ofekron/better-agent && cd better-agent && ./run.sh
# First run: pick a username + password (stored in your OS credential store),
# choose local-only or LAN access, then open the printed URL from any trusted device.
```

Full prerequisites and platform notes are in [Getting started](#getting-started).

---

## Why Better Agent

**Every provider, one workspace.** A session started with Claude sits next to
one running on Codex and another on Gemini — one history, one set of projects,
folders, tags, and search. Pick provider and model per session; nothing about
your workflow changes.

**Close the laptop — the work continues.** Agents run as detached processes
that outlive your browser tab, your frontend, even a backend restart. Come back
to the completed run, with the full trace of what happened while you were gone.
Crash-recoverable by design: a provider CLI that dies mid-turn is replayed on
restart and reconciled into the same session, deduplicated — live work and
recovered work converge to one history.

**Work from anywhere in the house.** The backend runs once on your machine; the
UI reaches it from your browser, the desktop app, or the mobile app on devices
you trust. Start a task at your desk, check on it from the couch — and keep the
backend off untrusted networks; every endpoint is credential-gated.

**A team, not a chatbot.** Fork sessions, delegate work, run agents in
parallel, and drive the whole thing headlessly from scripts through the
Integration SDK and CLI.

**Yours, actually.** Sessions, settings, credentials, and data live on your
machine, gated by credentials in your OS credential store. Better Agent hosts
nothing — the only optional outbound call is installing a marketplace extension
from a marketplace you choose.

**See everything your agents do.** Live output, every tool call, file context,
and full session history in one place — grouped by turns, your prompt alongside
the agent's answer. No hidden logs, no scrollback archaeology.

## vs. one agent in a terminal

| | One agent in a terminal | Better Agent |
| --- | --- | --- |
| Providers | One | Claude + Codex + Gemini, one workspace |
| Close the laptop | Session dies, or you hope `--resume` works | Detached runner keeps going; full trace on return |
| From your phone | No | Yes — same backend from mobile, desktop, or browser |
| Parallel work | Subagents, one provider | Fork + delegate across providers, headless via SDK/CLI |

---

## What's in the box

- **Guided provider setup** — connect the AI accounts and CLIs you already
  have; provider and model picked per session.
- **Offline-first capture** — type prompts and create sessions even while the
  backend is unreachable; they queue locally and sync when it's back.
- **Organization that scales** — project folder trees, tags, session tabs,
  quick filter, and advanced search across names, tags, folders, providers,
  and remembered context.
- **Project memory** — sessions organized by working directory, easy to reopen,
  fork, and continue.
- **Every surface** — browser, desktop app (macOS verified; Windows installer
  provided, not yet validated on a real Windows host), and Capacitor mobile
  apps, all backed by the same local server.
- **Extension surface** — private or marketplace extensions add specialized
  workflows on top of the core app; marketplace artifacts are
  signature-verified. See [EXTENSIONS.md](EXTENSIONS.md) for authoring your own
  and installing marketplace ones.

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
   FastAPI backend  ──►  per-session Claude / Codex / Gemini runners (detached)
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

**Prerequisites:** macOS, Linux, or Windows — auth is stored in your OS credential store (macOS Keychain / Windows Credential Manager / Linux Secret Service). Python 3 with a `backend/.venv`, Node.js, and a provider CLI such as Claude, Codex, or Gemini. The macOS desktop build is verified; the Windows installer is provided but not yet validated on a real Windows host. See `INSTALL.md` for the full clean-clone, LAN, desktop, mobile, and reset-auth flow.

```bash
# One command — builds the frontend, starts the backend on :8000, serves the UI.
./run.sh
```

First run prompts once for a username + password (stored only in your OS credential store) and asks whether Better Agent should listen only on this computer or on your local network. Open the printed URL from any trusted device that can reach the backend.

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
`CONTRIBUTING.md` before submitting changes. Pull requests go to the public
GitHub repository. See `RELEASE.md` for release integrity requirements and
`ROADMAP.md` for the public non-commercial roadmap.

---

**Better Agent — one agent in a terminal was never going to be enough.**

Ready? [`./run.sh`](#quickstart) — your first session is a minute away.

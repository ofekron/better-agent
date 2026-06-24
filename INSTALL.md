# Install Better Agent

Better Agent is source-available local software for coding agents. It can run
provider CLIs, execute commands, read and write files, and persist session data.
Use it only on trusted machines and networks.

## Prerequisites

- macOS for the current first-run auth flow.
- Python 3 with a virtualenv at `backend/.venv`.
- Node.js and npm.
- At least one provider CLI or compatible provider setup, such as Claude,
  Codex, or Gemini.

## Clean Clone

```bash
git clone git@gitlab.com:better-agent/better-agent.git
cd better-agent
```

Do not publish or zip a working tree that contains local nested repos such as
`better-agent-private/`, local virtualenvs, build output, `.better-claude/`, or
download artifacts.

## One-Command Run

```bash
./run.sh
```

First run prompts for a username and password. Credentials are stored through
the platform credential store. Setup also asks whether the backend should listen
only on this computer or on the trusted local network.

## Local-Only Vs LAN

- Local-only is the safer default.
- LAN mode is for trusted devices on your network.
- Never expose the backend to the public internet.
- All normal API access is gated after setup, but the server still controls
  real local tools and files.

## Desktop And Mobile

- Desktop packaging lives under `desktop/`.
- The frontend includes Capacitor Android/iOS project files under `frontend/`.
- Mobile clients point at the Better Agent server URL and use bearer auth after
  login.

## Manual Dev Run

```bash
cd backend
source .venv/bin/activate
uvicorn main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

## Reset Auth

```bash
./run.sh --reset-auth
```

## Read Before Shared Use

Read `DISCLAIMER.md`, `SECURITY.md`, `LICENSE`, `TRADEMARKS`, and `NOTICE`
before publishing, sharing, or accepting outside contributions.


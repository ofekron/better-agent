# Install Better Agent

Better Agent is source-available local software for coding agents. It runs
provider CLIs, executes commands, reads and writes files, and persists session
data. Use it only on trusted machines and networks.

**How to use this guide:** everyone does **Part 1 — Base setup**. Then install
only the optional modules you actually need — pick the surface, providers, and
network mode that match your machine. Skip anything that doesn't apply (no
Windows `.exe` build on a Mac, no Android SDK if you carry an iPhone, no Codex
CLI if you only use Claude).

---

## Part 1 — Base setup (everyone)

This gets the backend + frontend running in your browser. It's the only part
that is mandatory.

### 1.1 Prerequisites

On an empty Mac after cloning the repo, run:

```bash
./scripts/bootstrap-macos.sh
```

If `git` itself is missing, run `xcode-select --install` first, then clone and
run the bootstrap script. To install a provider CLI too, add
`--with-claude` or `--with-codex`.

| Tool | Why | Install |
| --- | --- | --- |
| **macOS** | First-run auth uses the login keychain | — |
| **Python 3.11+** | Backend runtime | `brew install python` |
| **uv** | Creates the venv and installs backend deps | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Node.js + npm** | Builds/serves the frontend | `brew install node` |
| **≥1 provider CLI** | The agent you actually talk to | See **Part 3** |

### 1.2 Clone

```bash
git clone git@gitlab.com:better-agent/better-agent.git
cd better-agent
```

Do not publish or zip a working tree that contains local nested repos such as
`better-agent-private/`, local virtualenvs, build output, `.better-claude/`, or
download artifacts.

### 1.3 Run

```bash
./run.sh
```

`run.sh` initializes submodules, installs Node deps, creates/syncs the backend
venv, installs the `bagent` CLI, builds the frontend, and serves everything on
`http://127.0.0.1:18765` by default.

First run prompts once for a **username + password** (stored only in your macOS
keychain) and asks whether the backend should listen **only on this computer**
or **on your trusted local network**. Open the printed URL.

> Reset stored credentials anytime: `./run.sh --reset-auth`

That's the whole base install. Everything below is optional.

---

## Part 2 — Pick your surface

How do you want to *use* Better Agent? Install only the matching module.

### Option A — Browser (default, nothing to install)

Already done by Part 1. Open the printed URL on this machine, or from another
trusted LAN device if you chose LAN mode. Stop here if the browser is enough.

### Option B — Install as a PWA — **app-like, zero build, any OS**

Better Agent is a Progressive Web App, so you can get an installed, standalone
app icon on **desktop, iPhone, and Android without building anything** — no
PyInstaller, no Xcode, no Android Studio. This is the recommended way to get an
"app" on most devices; only fall through to Options C/D if you specifically need
a native binary.

Just open the server URL in a browser and install it:

- **Desktop (Chrome/Edge):** the install icon in the address bar → *Install*.
- **iPhone/iPad (Safari):** Share → *Add to Home Screen*.
- **Android (Chrome):** menu → *Install app* / *Add to Home Screen*.

It launches full-screen, points at your existing backend, and updates itself
when you redeploy the frontend. For phones, this needs **LAN mode** (Part 4) so
the device can reach your backend.

### Option C — Native desktop app — **build only the one for YOUR OS**

The desktop app is a packaged shell around the same local backend. Each OS has
its own build script; build the one matching the machine you're on. Do **not**
run the Windows build on macOS or vice versa.

**On macOS → build the `.app` / `.dmg`:**
```bash
desktop/build_macos.sh
```
Needs: Xcode command-line tools (`xcode-select --install`). The script adds
`pyinstaller pywebview tufup` into the venv itself. Output is ad-hoc signed —
fine to run on the build machine; distributing to others needs a Developer ID
cert + notarization (out of scope).

**On Windows → build the installer:**
```powershell
powershell -ExecutionPolicy Bypass -File desktop\build_windows.ps1
```
Needs: a Windows host, [Inno Setup](https://jrsoftware.org/isinfo.php) (`ISCC`
on PATH) for the installer, and optionally an Authenticode cert
(`BA_SIGN_THUMBPRINT`) to sign. Skip this entirely if you're on macOS.

### Option D — Native mobile app — **install only Android OR iOS, matching your phone**

Only needed if the PWA (Option B) isn't enough — e.g. you need native speech
recognition or deeper OS integration. The mobile clients are Capacitor wrappers
that point at your Better Agent server URL. Build for the platform your phone
runs — there is no reason to install the
Android toolchain for an iPhone or Xcode for an Android phone.

**iPhone/iPad → iOS (requires macOS):**
- Needs: Xcode + CocoaPods (`sudo gem install cocoapods`).
- `cd frontend && npm install`
- Dev build pointed at this Mac's LAN IP, opens Xcode:
  ```bash
  npm run cap:dev:ios
  ```

**Android phone → Android:**
- Needs: Android Studio (bundles the Android SDK, `adb`, and a JDK). No Xcode,
  no CocoaPods.
- `cd frontend && npm install`
- Dev build pointed at this machine's LAN IP, opens Android Studio:
  ```bash
  npm run cap:dev:android
  ```

> The `cap:prod:*` scripts bake in a fixed server URL; edit that URL in
> `frontend/package.json` before using them.

---

## Part 3 — Pick your providers — **install only the CLIs you use**

You only need the CLI for the agent(s) you actually use. One is enough.

**Easiest path — the in-app provider setup wizard.** After Part 1, open the UI
and use the provider setup screen. It detects what's installed and installs the
ones you pick for you (with prerequisite checks). It covers:

| Provider | Installed via | Prereq | Auth |
| --- | --- | --- | --- |
| **Claude Code** | `npm install -g @anthropic-ai/claude-code` | npm | `claude` → `/login` |
| **Codex CLI** | `npm install -g @openai/codex` | npm | `codex login` |
| **Antigravity (`agy`)** | vendor install script | bash / PowerShell | per CLI prompts |
| **GitHub Copilot CLI** | `brew install copilot-cli` | Homebrew | `gh auth login` |

**Gemini** is also a supported provider but is **not** in the wizard — install
its CLI yourself (`gemini-cli`) and authenticate before selecting it.

Install only what you use: if you only run Claude, install only Claude and skip
the rest. After install + auth, pick the provider and model per session in the
UI.

---

## Part 4 — Network mode

Chosen during first run; change later via the in-app setup / `user_prefs.json`.

- **Local-only (`127.0.0.1`)** — safest default. Only this computer reaches the
  backend.
- **LAN (`0.0.0.0`)** — for trusted devices on your own network (a second
  computer, your phone). Required if you use the mobile app over Wi-Fi.
- **Never** expose the backend to the public internet. API access is gated after
  setup, but the server still drives real local tools and files.

---

## Part 5 — For agents working in this repo

Operational rules for an AI agent setting up, running, or editing Better Agent.

**Running the servers:**
- Base run: `./run.sh` (prod mode, serves built frontend on `:18765` by default).
- Dev split, hot-reload:
  ```bash
  cd backend && source .venv/bin/activate && uvicorn main:app --reload --port 8000
  cd frontend && npm run dev      # Vite on :5173
  ```
- **Never RESTART an already-running backend or frontend dev server without
  explicit user approval** — for any provider. Starting one that isn't running
  is fine.

**State, logs, and isolation:**
- All persistent state lives under `paths.ba_home()` (honors
  `BETTER_AGENT_HOME`, legacy `BETTER_CLAUDE_HOME`, default `~/.better-claude`).
  **Never** touch `~/.better-claude/` or `~/.better-agent/` directly in code,
  scripts, or shell — always go through `paths.ba_home()`.
- Backend run log: `ba_home()/backend-run.log`.
- In tests/scripts, set `BETTER_AGENT_HOME` to a fresh tempdir **before**
  importing any backend module, and `rmtree` it on exit. Never `rm -rf` a real
  state dir.

**Headless CLI driver (no browser):**
```bash
cd backend && source .venv/bin/activate
python cli.py                    # interactive REPL
python cli.py -p "do the thing"  # one-shot
python cli.py --json -p "..."    # jsonl output for scripting
```

**Tests** (real provider CLI subprocesses — slow, catch wiring bugs):
```bash
cd backend && source .venv/bin/activate
python scripts/integration_test_<thing>.py
```

**Parity rule:** when changing a provider-facing or desktop feature, apply the
equivalent change for every supported provider (Claude/Codex/Gemini) and for
both macOS and Windows in the same change, or stop and ask.

**Deeper architecture:** see the `project-structure` skill at
`.claude/skills/project-structure/` and `CLAUDE.md`.

---

## Read before shared use

Read `DISCLAIMER.md`, `SECURITY.md`, `LICENSE`, `TRADEMARKS`, and `NOTICE`
before publishing, sharing, or accepting outside contributions.

# Extensions: local and remote

Better Agent is extended through self-contained **extension packages**. An
extension can add UI, backend HTTP routes, MCP tools, instructions, and skills.
This document covers the two ways an extension reaches your machine — **local**
(your own code, kept private) and **remote** (signed artifacts from the
marketplace) — and the trust boundary that separates them.

The mental model:

- **Local extensions live in a repo you control.** Better Agent snapshots them
  into its state dir at runtime and re-syncs when they change. Your source never
  enters the Better Agent repo or any public place.
- **Remote extensions are downloaded as signed artifacts.** Their code is
  fetched on install and verified against a pinned key; you never need their
  source.
- **The trust boundary is enforced by source type, not by the package.** An
  extension cannot declare itself trusted — trust is bound to *how it was
  installed*.

---

## 1. The trust boundary (read this first)

Every installed extension has a **source type** that determines what it is
allowed to do. The source type is set by the installer and is never read from
the package, so a shipped extension cannot forge it
(`backend/extension_store.py`, `is_first_party`).

| Source type | How it gets installed | Trust | In-process | Consent |
|---|---|---|---|---|
| `better_agent_bundled` | Ships in a Better Agent release | First-party | yes | exempt |
| `better_agent_local` | Snapshotted from a local repo on your machine | First-party | yes | exempt |
| `better_agent_signed` | Signed artifact from the marketplace | First-party | yes | exempt |
| `marketplace` / `git` / `artifact` | Third-party install | Third-party | **no** | required |

First-party extensions are consent-exempt and are the **only** extensions
allowed to run in-process or declare `backend_routes` / `internal_loopback`
permissions. Third-party extensions always require explicit consent and never
run in-process.

This is what lets you treat your own extensions the same way the maintainer
treats his: anything you install from a local repo you control is first-party.

---

## 2. Local extensions (your own code, kept private)

A local extension is just a directory with a manifest. Better Agent discovers
it, copies a snapshot into its state dir, and re-syncs when the source changes.

### Where Better Agent looks

`_local_private_extension_repo_root()` resolves your extensions root in this
order:

1. `BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH` — explicit path to your
   extensions repo.
2. `<better-agent-repo>/better-agent-private/` — if that sibling dir exists.
3. `<better-agent-repo>/` itself.

Under whichever root wins, Better Agent scans
`extensions/*/better-agent-extension.json` and installs every package it finds
as `better_agent_local` (first-party).

So as a cloner you have two equivalent options:

- **Keep extensions in your own repo** and point at it:
  ```bash
  export BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH=/path/to/my-extensions
  ```
  Your repo: `my-extensions/extensions/<id>/better-agent-extension.json`.
- **Or drop them into your Better Agent checkout** at
  `extensions/<id>/better-agent-extension.json` (fallback 3 above).

Either way the source stays in **your** repo. Nothing is copied into the public
Better Agent repo, and nothing is uploaded anywhere.

### Required layout

```
my-extensions/
  extensions/
    <your.id>/
      better-agent-extension.json     # manifest (required)
      ui/                             # frontend assets (optional)
      backend/                        # python backend module (optional)
      mcp/                            # MCP server (optional)
```

### How syncing works

- On startup (and on reconcile), Better Agent snapshots each local package into
  `$BETTER_AGENT_HOME/extensions/installed/<id>/versions/<sha>/`.
- The version key is the **git HEAD commit** of your extensions repo
  (`_private_extension_commit_sha`). When you commit a change, the next
  reconcile detects the new HEAD and re-snapshots — edits take effect on the
  next store reconcile without a manual reinstall.
- Local extensions are first-party, so they are enabled without a consent
  prompt and may use `backend_routes` / `internal_loopback`.

> State isolation: all snapshots live under `$BETTER_AGENT_HOME` (defaults to
> `~/.better-claude`). Never write scripts that touch that path directly — go
> through Better Agent's own paths/config.

### Minimal manifest

```json
{
  "kind": "better-agent-extension",
  "id": "acme.helpers",
  "name": "Acme Helpers",
  "version": "0.1.0",
  "description": "My private helpers.",
  "surfaces": ["backend_feature", "frontend_feature"],
  "entrypoints": {
    "frontend": "ui/index.html",
    "frontend_modules": [
      {
        "slot": "settings",
        "id": "helpers",
        "label": "Acme Helpers",
        "kind": "iframe",
        "module": "ui/index.html"
      }
    ],
    "backend_module": "backend.routes"
  },
  "permissions": { "backend_routes": true, "internal_loopback": true },
  "protocol": {
    "version": 1,
    "smoke_test": {
      "required_paths": ["better-agent-extension.json", "ui/index.html", "backend/routes.py"],
      "python_modules": ["backend.routes"]
    }
  }
}
```

`id` rules: 3–80 chars, lowercase, `[a-z0-9._-]`. Use a namespace prefix you
own (`acme.`, not `ofek-dev.` or `better-agent.`).

---

## 3. Surfaces and entrypoints

`surfaces` is a subset of `{backend_feature, frontend_feature, runtime_mcp,
instructions, skills}` (`_ALLOWED_SURFACES`). Entrypoints declare the concrete
implementations.

### Frontend (`frontend_feature`)

- `entrypoints.frontend` — the HTML asset root.
- `entrypoints.frontend_modules` — list of `{ slot, id, label, kind, module }`.
  - `slot`: `settings` (a new Settings page section), a quick button, or a page.
  - `kind`:
    - `iframe` — a self-contained HTML page rendered in a sandboxed iframe.
      This is the simplest way to ship a packaged UI with no build step.
    - `module` — a bundled `.entry.js` dynamically imported into the app.

Frontend assets are served at
`/api/extensions/<id>/frontend/<path>` (`resolve_frontend_asset`). Asset paths
are confined under the frontend directory; traversal is rejected.

### Backend (`backend_feature`)

- `entrypoints.backend_module` — a python module path (e.g. `backend.routes`)
  exporting `create_router(context) -> fastapi.APIRouter`. It is mounted at
  `/api/extensions/<id>/backend/*`. Requires `permissions.backend_routes: true`.

### MCP (`runtime_mcp`)

- `entrypoints.mcp` — one or more MCP server configs (`python` script, `args`,
  `env`). Requires `permissions.internal_loopback` to receive the internal
  backend token.

### Instructions / skills (`instructions`, `skills`)

Inject managed instruction blocks or contribute skills. These surfaces don't
execute code.

### Permissions

Declared in `permissions` as booleans, `"optional"`, or scoped lists. Sensitive
capabilities (`backend_routes`, `internal_loopback`, `in_process_execution`,
`secrets`, `filesystem`, …) are honored only for first-party extensions;
third-party extensions must request them and win consent. Changing the declared
set invalidates prior consent (`permission_consent_fingerprint`).

---

## 4. Remote extensions (marketplace)

Remote extensions are distributed as **signed artifacts**. You browse a catalog,
pick an extension, and Better Agent downloads the artifact and verifies its
signature before installing.

### Install flow

1. Catalog fetch: `GET <marketplace>/extensions.json` →
   `{ "extensions": [ { id, name, version, …, metadata_url } ] }`.
2. Per-extension metadata: `GET <marketplace>/extensions/<id>/metadata` →
   `{ artifact_url, artifact_sha256, signature, signature_alg }`.
3. Install: `POST /api/extensions/install` with the metadata. Better Agent
   downloads the artifact and verifies it.

### Signature trust model (fail-closed)

`_verify_artifact_signature` trusts **only the pinned public key** — never a
key supplied by the metadata. This means an attacker who tampered with the
metadata cannot ship a malicious artifact plus a matching key and self-validate
it. Specifically:

- The pinned key comes from `BETTER_AGENT_MARKETPLACE_PUBLIC_KEY` (or the
  built-in default). A metadata-supplied key is ignored.
- Signature is Ed25519 over `{ artifact_sha256, extension_id, version }`.
- `_validate_artifact_url` requires `https://` and rejects embedded credentials
  (`BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS=1` opts in to insecure,
  for local testing only).
- `_download_artifact` caps size at 50 MiB (`_MAX_ARTIFACT_BYTES`), rejects
  symlinks, and blocks path traversal (`..`, absolute paths) on extraction.
- Any failure raises and the install is aborted. Nothing partial is kept.

Installed marketplace artifacts are `better_agent_signed` (first-party) **only**
when they verify against the pinned key. Unsigned/third-party installs are
`marketplace`/`git`/`artifact` source types — third-party, consent-gated, never
in-process.

### Configuration

| Env var | Purpose |
|---|---|
| `BETTER_AGENT_MARKETPLACE_BASE_URL` | Catalog/metadata/artifact base URL. |
| `BETTER_AGENT_MARKETPLACE_PUBLIC_KEY` | Override the pinned signing key. |
| `BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS` | `1` to allow non-https artifact URLs (dev only). |

---

## 5. Keeping commercial secrets in the right boundary

The boundary is mechanical, not social:

1. **Your private source stays in your repo.** Local extensions are read from
   your extensions repo and snapshotted into your local `$BETTER_AGENT_HOME`
   only. Better Agent has no upload path for local extension source.
2. **The marketplace is a one-way publish for *you*.** If you choose to
   distribute an extension, you sign artifacts and host them yourself; clients
   verify against the pinned key. You decide what to publish — unpublished
   extensions in your local repo are never reachable by anyone else.
3. **Trust is not self-declarable.** A package cannot elevate itself to
   first-party or grant itself `backend_routes`/in-process execution by editing
   its manifest. Only the install path grants trust.
4. **Namespace your ids.** Use your own prefix (`acme.*`). The `ofek-dev.*` and
   `better-agent.*` namespaces are reserved; colliding ids can be obsoleted.

---

## 6. Quick start for a cloner

```bash
# 1. Make an extensions repo
mkdir -p my-ext/extensions/acme.helpers/ui
cat > my-ext/extensions/acme.helpers/better-agent-extension.json <<'JSON'
{ "kind": "better-agent-extension", "id": "acme.helpers", "name": "Helpers",
  "version": "0.1.0", "surfaces": ["frontend_feature"],
  "entrypoints": { "frontend": "ui/index.html",
    "frontend_modules": [{ "slot": "settings", "id": "helpers",
      "label": "Helpers", "kind": "iframe", "module": "ui/index.html" }] } }
JSON
echo '<!doctype html><h1>Helpers</h1>' > my-ext/extensions/acme.helpers/ui/index.html

# 2. Point Better Agent at it
export BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH="$PWD/my-ext"

# 3. (Re)start the backend. A "Helpers" section appears in Settings.
```

To browse and install public extensions instead, open the **Marketplace**
section in Settings (or use the `ofek-dev.marketplace` MCP tools) and install
from the catalog — they are verified against the pinned signing key on the way
in.

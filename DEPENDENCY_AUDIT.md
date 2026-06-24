# Dependency And License Audit

Last local audit: 2026-06-22.

## Commands Run

- Node lockfile scan over `package-lock.json`, `frontend/package-lock.json`, and
  `provider-config-sync/package-lock.json`.
- Python distribution metadata scan from the current `backend/.venv`.
- Current-tree secret pattern scan for common private keys, API tokens, GitHub
  tokens, GitLab tokens, Slack tokens, AWS keys, and OpenAI-style keys.
- Git history path scan for private paths, `.env` files, key files, and release
  artifacts.

## Node Result

- Root `package-lock.json`: no GPL/AGPL/SSPL/BUSL/PolyForm/Commons-Clause style
  licenses detected.
- `frontend/package-lock.json`: no GPL/AGPL/SSPL/BUSL/PolyForm/Commons-Clause
  style licenses detected.
- `provider-config-sync/package-lock.json`: no suspicious dependency licenses
  detected; local workspace package entries do not include license fields.

## Python Result

The current `backend/.venv` is not a clean source-of-truth lock. It contains
packages that may be local tooling or unrelated to the published dependency
set. The scan flagged GPL/LGPL/PolyForm-style metadata in the venv, including
`fs_sdk`, `MouseInfo`, `edge-tts`, `pynput`, `pyphen`, `python-bidi`, and
PyInstaller-related packages.

Before public release, rerun Python license audit from a fresh environment built
only from the published dependency files and decide whether each flagged package
is a runtime dependency, build-only dependency, removable dependency, or allowed
under the source-available distribution model after legal review.

## Current Blocking Notes

- Dedicated scanners (`gitleaks`, `trufflehog`, `detect-secrets`, `git-secrets`)
  were not installed in this environment.
- Public git history still contains prior `marketing/linkedin-posts/*` drafts
  and old `marketing/better-agent/downloads/*` artifacts. Removing them requires
  an explicit coordinated history rewrite and force-push.
- Hosted GitLab settings could not be configured here because `glab` has no API
  token in this environment.


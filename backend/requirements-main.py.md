# Requirements for main.py

Source of truth: user prompts only. Do not add anything not literally stated by the user.

## Requirements

- [2026-05-27] On Ctrl+C / SIGINT in an interactive terminal, `on_shutdown` MUST prompt the user "kill running claude processes? [Y/n]" and only call `provider.cancel_all()` when the user accepts. Decision matrix:
  - Enter / explicit "y" / non-TTY / EOF → kill (default, unchanged for non-interactive).
  - Explicit "n" / "no" → don't kill.
  - **A second Ctrl+C** while the prompt is showing → don't kill (treated as "n").
  - Source (amends 2026-05-23): "ctrl c twice should be working like n e.g dont kill subprocesses"
  - Why: users double-tap Ctrl+C impulsively to "make it stop". The safer interpretation is "stop the backend but leave my long-running Claude/Gemini runs alone" — `run_recovery` rehooks them on next start. Non-interactive contexts (desktop `.app`'s SIGINT path) still default to kill — there's no Ctrl+C to double-tap there.

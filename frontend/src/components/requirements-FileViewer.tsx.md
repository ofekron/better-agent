# Requirements for FileViewer.tsx

Source of truth: user prompts only. Do not add anything not literally stated by the user.

## Requirements

- [2026-05-14] FR-FILE.0.1 (tightened) — when the user enters a File-Mode session (selecting an existing file-edit session counts as "entry", as does creating one), the file viewer MUST auto-open: panel visible, file loaded, diff baseline computed. User MUST NOT have to manually open or reveal it.
  - Source: prompt [29] "file panel doesn't open"; clarification "entering a session for file edit is entry here... make it clear"
- [2026-05-14] For .md files: render markdown formatted by default. Double-clicking the rendered view enters Monaco raw-edit mode. Auto-save throughout editing (debounced). 10 seconds after the LAST keystroke, flip back to the formatted view.
  - Source: "if its md it should apply the tags formatting, and enter edit mode when clicking twice somewhere, then count 10 seconds debounced from last edit to get back to view formatted"; clarification "Same, but auto-save throughout editing"

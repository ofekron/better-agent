# Requirements for FileEditor.tsx

Source of truth: user prompts only. Do not add anything not literally stated by the user.

## Requirements

- [2026-05-14] For .md files: render markdown formatted by default. Double-clicking the rendered view enters Monaco raw-edit mode. Auto-save throughout editing (debounced). 10 seconds after the LAST keystroke, flip back to the formatted view.
  - Source: "if its md it should apply the tags formatting, and enter edit mode when clicking twice somewhere, then count 10 seconds debounced from last edit to get back to view formatted"; clarifications "Same, but auto-save throughout editing" + "Both FileViewer and FileEditor"
- [2026-05-14] Default view mode is "File" (single-editor), not "Diff" (side-by-side). Applies to all file editors with a diff/file toggle.
  - Source: "file editor should start in file mode not in diff mode diff will most likely be used less"; clarification "in all file editors default view = 'File' mode, not 'Diff'"

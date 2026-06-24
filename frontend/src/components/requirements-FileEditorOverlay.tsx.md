# Requirements for FileEditorOverlay.tsx

Source of truth: user prompts only. Do not add anything not literally stated by the user.

## Requirements

- [2026-05-14] While the file-edit overlay is active, the outer sidebar (projects + sessions panel) MUST be trimmed to a smaller width to give the chat + file panels more room.
  - Source: "in file edit mode we need more space so we can trim the left panel to smaller panel"
- [2026-05-14] The inner chat-vs-file divider MUST start at 50/50 (chat 50%, file 50%) on first open. The divider remains user-draggable so the file side can be enlarged further.
  - Source: "the divider start at 50 50 file and session panels, when file side can be enlarged as well"

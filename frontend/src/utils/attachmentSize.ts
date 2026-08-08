// Single formatter for a byte count into a human-readable attachment size
// label (`.message-file-size`) — shared by legacy's `MessageBubble.tsx`
// `UserFiles` and the native `surface/leaf/AttachmentChips.tsx`, so both
// paths render the exact same "12.3 KB"/"1.4 MB" text for the same bytes.
export function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

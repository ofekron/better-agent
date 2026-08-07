// Shared markdown leaf — wraps the app's existing SafeMarkdownPreview
// (components/SafeMarkdownPreview.tsx), which is already shape-agnostic
// (no ChatMessage/WSEvent coupling) and safe to import into surface/
// as-is: single source of truth for "how markdown renders" across both
// the legacy and native render paths.

import { SafeMarkdownPreview } from "../../components/SafeMarkdownPreview";

export function Markdown({ text, className }: { text: string; className?: string }) {
  if (!text) return null;
  return (
    <SafeMarkdownPreview
      source={text}
      className={className ? `surface-markdown ${className}` : "surface-markdown"}
      style={{ background: "transparent", color: "inherit", fontFamily: "inherit" }}
    />
  );
}

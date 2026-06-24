/** Send-time snapshot of one file panel the user has open. `visible`
 * / `selection` are read live from the mounted Monaco editor at the
 * moment the user hits send — never persisted (state-ownership rule).
 * Both are null when the panel isn't a mounted Monaco view (still
 * loading, inactive tab, or a markdown/csv renderer). */
export interface OpenFileSnapshot {
  path: string;
  visible: { startLine: number; endLine: number } | null;
  caret: { line: number; column: number } | null;
  selection: {
    startLine: number;
    endLine: number;
    startColumn?: number;
    endColumn?: number;
  } | null;
}

function rangeForSelection(s: NonNullable<OpenFileSnapshot["selection"]>): string {
  if (s.startColumn !== undefined && s.endColumn !== undefined) {
    return `${s.startLine}:${s.startColumn}-${s.endLine}:${s.endColumn}`;
  }
  return `${s.startLine}-${s.endLine}`;
}

function lineForSnapshot(s: OpenFileSnapshot): string {
  const parts: string[] = [];
  if (s.visible) {
    parts.push(`view ${s.visible.startLine}-${s.visible.endLine}`);
  }
  if (s.caret) {
    parts.push(`caret ${s.caret.line}:${s.caret.column}`);
  }
  if (s.selection) {
    parts.push(`selection ${rangeForSelection(s.selection)}`);
  }
  const detail = parts.length > 0 ? ` (${parts.join(", ")})` : "";
  return `- ${s.path}${detail}`;
}

/** Build the "files the user has open" system-reminder preamble.
 * Returns "" when nothing is open so the caller can skip prepending.
 * Mirrors the inline-tags preamble mechanism (client-side, handleSend
 * only, re-snapshotted every send for as long as the file stays
 * open). */
export function buildOpenFilesPreamble(snapshots: OpenFileSnapshot[]): string {
  if (snapshots.length === 0) return "";
  const lines = snapshots.map(lineForSnapshot).join("\n");
  return (
    "<system-reminder>\n" +
    "Open files in the user's UI; line/column numbers are 1-based.\n" +
    lines +
    "\n</system-reminder>"
  );
}

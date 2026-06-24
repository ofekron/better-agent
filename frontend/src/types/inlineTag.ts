export interface FileAnchor {
  filePath: string;
  // Line/col are present when the selection came from a Monaco editor
  // (eng overlay or FileViewer's code/json/diff modes). Absent when the
  // selection came from a non-Monaco rendered view (markdown HTML,
  // CSV/TSV table) — in that case `selectedText` on the InlineTag is
  // the only positional information.
  startLine?: number;
  endLine?: number;
  startCol?: number;
  endCol?: number;
}

/** Per-session tag a user attaches via SelectionPopup, the eng overlay's
 * FileEditor, or the right-panel FileViewer.
 *
 * Three flavors share this shape:
 *
 * 1. **Message-anchored** (chat messages): `messageId` points at an
 *    assistant message DOM node. `selectedText` is the copied span;
 *    `fileAnchor` is undefined.
 *
 * 2. **File-anchored, line:col** (Monaco views): `fileAnchor` carries
 *    `{filePath, startLine, endLine, startCol, endCol}`. `selectedText`
 *    may be empty (live content can drift). `messageId` is a synthetic
 *    placeholder.
 *
 * 3. **File-anchored, text-only** (rendered markdown / CSV / TSV):
 *    `fileAnchor` carries only `filePath` (no line:col). `selectedText`
 *    is the snippet copied from the rendered DOM. `messageId` is a
 *    synthetic placeholder.
 *
 * All flavors batch the same way: tags accumulate in
 * `session.inline_tags[]` and are merged into the next outgoing prompt
 * as a preamble. Cleared after the user clicks Send.
 */
export interface InlineTag {
  id: string;
  messageId: string;
  selectedText: string;
  comment: string;
  timestamp: string;
  fileAnchor?: FileAnchor;
  /** Transient 1-based footnote number derived from the session's tag
   * order in App — shown in the comments panel and as the highlight's
   * reference marker. Never persisted. */
  displayNumber?: number;
}
